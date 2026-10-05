/*
 * TX 发包器：周期性 ESP-NOW 广播小包，供 RX 板提取 CSI。
 * 沿用 esp-csi csi_send 的验证过的模式：
 *   - 固定 MAC 1a:00:00:00:00:00（RX 按 MAC 过滤 CSI 来源）
 *   - 4 字节递增序号作载荷（RX 侧用它统计丢包率）
 *   - WIFI_PS_NONE（否则 modem-sleep 丢包）
 * 差异：速率/信道运行时可配（控制台），带宽 Kconfig 可配（默认 HT20）。
 * 仅支持 ESP32-S3（项目单目标，多目标分支已裁剪）。
 */
#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include <stdatomic.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "esp_mac.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_console.h"
#include "argtable3/argtable3.h"
#include "linenoise/linenoise.h"

static const char *TAG = "tx_sender";

static const uint8_t s_tx_mac[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
static const uint8_t s_bcast[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

#if CONFIG_TX_BANDWIDTH_HT40
#define TX_BW        WIFI_BW_HT40
#define TX_SECOND    WIFI_SECOND_CHAN_BELOW
#define TX_PHY_MODE  WIFI_PHY_MODE_HT40
#else
#define TX_BW        WIFI_BW_HT20
#define TX_SECOND    WIFI_SECOND_CHAN_NONE
#define TX_PHY_MODE  WIFI_PHY_MODE_HT20
#endif

static atomic_int s_rate_hz;
static volatile int s_channel = CONFIG_TX_DEFAULT_CHANNEL;   /* Kconfig 兜底，NVS 命中时覆盖 */
static volatile bool s_pause;      // 换信道期间暂停发包
static volatile uint32_t s_seq;
static volatile uint32_t s_send_err;

/* ---------- NVS 持久化（信道/速率，重启不回默认） ---------- */

static void nvs_load_config(void)
{
    nvs_handle_t h;
    if (nvs_open("txcfg", NVS_READONLY, &h) == ESP_OK) {
        int32_t v = 0;
        if (nvs_get_i32(h, "channel", &v) == ESP_OK && v >= 1 && v <= 13) {
            s_channel = (int)v;
        }
        if (nvs_get_i32(h, "rate", &v) == ESP_OK && v >= 10 && v <= 200) {
            atomic_store(&s_rate_hz, (int)v);
        }
        nvs_close(h);
    }
}

static void nvs_save(const char *key, int32_t val)
{
    nvs_handle_t h;
    if (nvs_open("txcfg", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_i32(h, key, val);
        nvs_commit(h);
        nvs_close(h);
    }
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, s_tx_mac));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, TX_BW));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(s_channel, TX_SECOND));
}

static void espnow_apply_peer(int ch)
{
    esp_now_peer_info_t peer = {
        .channel   = ch,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    esp_now_del_peer(s_bcast);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate_config = {
        .phymode = TX_PHY_MODE,
        .rate    = WIFI_PHY_RATE_MCS0_LGI,
        .ersu    = false,
        .dcm     = false,
    };
    /* peer 重建后速率配置清零，必须重挂（旧实现漏了这步） */
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(s_bcast, &rate_config));
}

static void espnow_init(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    espnow_apply_peer(s_channel);
}

static void change_channel(int ch)
{
    s_pause = true;
    vTaskDelay(pdMS_TO_TICKS(100));            /* 让在途 esp_now_send 完成（队列异步下发） */
    esp_err_t err = esp_wifi_set_channel(ch, TX_SECOND);
    uint8_t cur = 0;
    wifi_second_chan_t sec = WIFI_SECOND_CHAN_NONE;
    esp_wifi_get_channel(&cur, &sec);           /* 读回校验：射频是否真的切了 */
    if (err == ESP_OK && cur == (uint8_t)ch) {
        espnow_apply_peer(ch);
        s_channel = ch;
        nvs_save("channel", ch);
        ESP_LOGI(TAG, "channel -> %d (verified)", ch);
    } else {
        ESP_LOGE(TAG, "set channel %d failed: err=%s readback=%d", ch, esp_err_to_name(err), cur);
    }
    s_pause = false;
}

static void sender_task(void *arg)
{
    TickType_t last = xTaskGetTickCount();
    for (;;) {
        if (!s_pause) {
            uint32_t s = ++s_seq;
            if (esp_now_send(s_bcast, (const uint8_t *)&s, sizeof(s)) != ESP_OK) {
                s_send_err++;
            }
        }
        int hz = atomic_load(&s_rate_hz);
        vTaskDelayUntil(&last, pdMS_TO_TICKS(1000 / hz));
    }
}

/* ---------- 控制台命令 ---------- */

static struct {
    struct arg_int *hz;
    struct arg_end *end;
} rate_args;

static int cmd_rate(int argc, char **argv)
{
    int nerrors = arg_parse(argc, argv, (void **)&rate_args);
    if (nerrors != 0) {
        arg_print_errors(stderr, rate_args.end, argv[0]);
        return 1;
    }
    int hz = rate_args.hz->ival[0];
    if (hz < 10 || hz > 200) {
        printf("rate out of range [10,200]\n");
        return 1;
    }
    atomic_store(&s_rate_hz, hz);
    nvs_save("rate", hz);
    printf("rate -> %d Hz\n", hz);
    return 0;
}

static struct {
    struct arg_int *ch;
    struct arg_end *end;
} chan_args;

static int cmd_channel(int argc, char **argv)
{
    int nerrors = arg_parse(argc, argv, (void **)&chan_args);
    if (nerrors != 0) {
        arg_print_errors(stderr, chan_args.end, argv[0]);
        return 1;
    }
    int ch = chan_args.ch->ival[0];
    if (ch < 1 || ch > 13) {
        printf("channel out of range [1,13]\n");
        return 1;
    }
    change_channel(ch);
    return 0;
}

static int cmd_info(int argc, char **argv)
{
    printf("rate=%d Hz channel=%d bw=%s seq=%" PRIu32 " send_err=%" PRIu32 "\n",
           atomic_load(&s_rate_hz), s_channel,
           TX_BW == WIFI_BW_HT40 ? "HT40" : "HT20", s_seq, s_send_err);
    return 0;
}

static void console_init(void)
{
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_cfg = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    repl_cfg.prompt = "tx>";
    repl_cfg.max_cmdline_length = 64;
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG      /* 本板接的是 S3 原生 USB 口 */
    esp_console_dev_usb_serial_jtag_config_t dev_cfg = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&dev_cfg, &repl_cfg, &repl));
#else
    esp_console_dev_uart_config_t uart_cfg = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_uart(&uart_cfg, &repl_cfg, &repl));
#endif

    rate_args.hz = arg_int1(NULL, NULL, "<hz>", "send rate 10..200 Hz");
    rate_args.end = arg_end(1);
    const esp_console_cmd_t rate_cmd = {
        .command = "rate", .help = "set send rate (Hz)",
        .hint = NULL, .func = &cmd_rate, .argtable = &rate_args
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&rate_cmd));

    chan_args.ch = arg_int1(NULL, NULL, "<ch>", "channel 1..13");
    chan_args.end = arg_end(1);
    const esp_console_cmd_t chan_cmd = {
        .command = "channel", .help = "set wifi channel (pause sending during switch)",
        .hint = NULL, .func = &cmd_channel, .argtable = &chan_args
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&chan_cmd));

    const esp_console_cmd_t info_cmd = {
        .command = "info", .help = "show current config",
        .hint = NULL, .func = &cmd_info,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&info_cmd));

    ESP_ERROR_CHECK(esp_console_start_repl(repl));
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    atomic_init(&s_rate_hz, CONFIG_TX_DEFAULT_RATE_HZ);
    nvs_load_config();                        /* NVS 覆盖默认信道/速率 */
    wifi_init();
    espnow_init();

    ESP_LOGI(TAG, "==== TX sender ====");
    ESP_LOGI(TAG, "channel=%d rate=%d Hz bw=%s mac=" MACSTR,
             s_channel, atomic_load(&s_rate_hz),
             TX_BW == WIFI_BW_HT40 ? "HT40" : "HT20", MAC2STR(s_tx_mac));

    console_init();
    xTaskCreate(sender_task, "sender", 4096, NULL, 5, NULL);
}
