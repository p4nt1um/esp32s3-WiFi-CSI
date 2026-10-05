/*
 * RX 采集器：从 TX 板（MAC 1a:00:00:00:00:00）的 ESP-NOW 包提取 CSI，
 * 经环形缓冲转发到写盘任务，UART 输出 esp-csi 兼容 CSV（esp_csi_tool/解析器可直接读）。
 * 沿用 esp-csi csi_recv 的验证过的模式：
 *   - CSI 配置 S3 默认全开（lltf/htltf/stbc_htltf2/ltf_merge/channel_filter）
 *   - esp_csi_gain_ctrl 增益补偿（AGC 跳变会污染幅度，前 100 包记基线）
 *   - CSV 含 first_word_invalid 字段（缓冲首字无效问题）
 * 差异：环形缓冲解耦回调与打印（回调只打包，写任务格式化）；
 *       控制台命令 csv/channel/stats；MQTT 上行 Kconfig 可选（默认关）。
 * 仅支持 ESP32-S3。
 */
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_mac.h"
#include "rom/ets_sys.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_console.h"
#include "argtable3/argtable3.h"
#include "linenoise/linenoise.h"
#include "esp_csi_gain_ctrl.h"
#include "mqtt_pub.h"
#include "replay.h"

static const char *TAG = "rx_collector";

static const uint8_t s_tx_mac[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};

#if CONFIG_RX_BANDWIDTH_HT40
#define RX_BW        WIFI_BW_HT40
#define RX_SECOND    WIFI_SECOND_CHAN_BELOW
#define RX_PHY_MODE  WIFI_PHY_MODE_HT40
#else
#define RX_BW        WIFI_BW_HT20
#define RX_SECOND    WIFI_SECOND_CHAN_NONE
#define RX_PHY_MODE  WIFI_PHY_MODE_HT20
#endif

#define CSI_MAX_LEN 128

/* 写任务消费的 CSI 记录：携带 csi_recv S3 CSV 格式所需的全部字段 */
typedef struct {
    uint32_t seq;                 /* TX 载荷序号（payload+15，沿用上游偏移） */
    uint8_t  mac[6];
    int8_t   rssi;
    uint8_t  rate;
    uint8_t  sig_mode;
    uint8_t  mcs;
    uint8_t  cwb;
    uint8_t  smoothing;
    uint8_t  not_sounding;
    uint8_t  aggregation;
    uint8_t  stbc;
    uint8_t  fec_coding;
    uint8_t  sgi;
    int8_t   noise_floor;
    uint8_t  ampdu_cnt;
    uint8_t  channel;
    int8_t   secondary_channel;
    uint32_t timestamp;           /* local_timestamp (us) */
    uint8_t  ant;
    uint16_t sig_len;
    uint16_t len;                 /* CSI 字节数（HT20 预期 52×2=104，HT40 128×2=256>128 需截断） */
    uint8_t  first_word_invalid;
    float    gain;                /* 增益补偿系数 */
    int8_t   buf[CSI_MAX_LEN];
} csi_rec_t;

static RingbufHandle_t s_ring;
static volatile bool s_csv_on = CONFIG_RX_CSV_DEFAULT_ON;
static bool s_header_printed;
static volatile uint32_t s_rx_cnt;
static volatile uint32_t s_drop_cnt;

/* ---------------- CSI 回调（只打包，不打印） ---------------- */

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf) {
        return;
    }
    /* 只处理来自 TX 板的包 */
    if (memcmp(info->mac, s_tx_mac, 6)) {
        return;
    }

    /* 增益补偿：前 100 包记录基线（S3 AGC 跳变会污染幅度） */
    static uint32_t s_count = 0;
    float compensate_gain = 1.0f;
    static uint8_t agc_gain = 0;
    static int8_t fft_gain = 0;
    esp_csi_gain_ctrl_get_rx_gain(&info->rx_ctrl, &agc_gain, &fft_gain);
    if (s_count < 100) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
    } else if (s_count == 100) {
        static uint8_t agc_baseline = 0;
        static int8_t fft_baseline = 0;
        esp_csi_gain_ctrl_get_rx_gain_baseline(&agc_baseline, &fft_baseline);
    }
    esp_csi_gain_ctrl_get_gain_compensation(&compensate_gain, agc_gain, fft_gain);
    s_count++;

    csi_rec_t rec = { 0 };
    rec.seq = *(uint32_t *)(info->payload + 15);
    memcpy(rec.mac, info->mac, 6);
    const wifi_pkt_rx_ctrl_t *r = &info->rx_ctrl;
    rec.rssi = r->rssi;
    rec.rate = r->rate;
    rec.sig_mode = r->sig_mode;
    rec.mcs = r->mcs;
    rec.cwb = r->cwb;
    rec.smoothing = r->smoothing;
    rec.not_sounding = r->not_sounding;
    rec.aggregation = r->aggregation;
    rec.stbc = r->stbc;
    rec.fec_coding = r->fec_coding;
    rec.sgi = r->sgi;
    rec.noise_floor = r->noise_floor;
    rec.ampdu_cnt = r->ampdu_cnt;
    rec.channel = r->channel;
    rec.secondary_channel = r->secondary_channel;
    rec.timestamp = r->timestamp;
    rec.ant = r->ant;
    rec.sig_len = r->sig_len;
    rec.len = info->len > CSI_MAX_LEN ? CSI_MAX_LEN : info->len;
    rec.first_word_invalid = info->first_word_invalid;
    rec.gain = compensate_gain;
    memcpy(rec.buf, info->buf, rec.len);

    if (xRingbufferSend(s_ring, &rec, sizeof(rec), 0) != pdTRUE) {
        s_drop_cnt++;   /* 缓冲满：写任务跟不上时丢新包并计数 */
    }
}

/* ---------------- 写任务：格式化 CSV + MQTT 累积 ---------------- */

static void print_csv_line(const csi_rec_t *r)
{
    ets_printf("CSI_DATA,%" PRIu32 "," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%" PRIu32 ",%d,%d,%d",
               r->seq, MAC2STR(r->mac), r->rssi, r->rate, r->sig_mode, r->mcs, r->cwb,
               r->smoothing, r->not_sounding, r->aggregation, r->stbc, r->fec_coding,
               r->sgi, r->noise_floor, r->ampdu_cnt, r->channel, r->secondary_channel,
               r->timestamp, r->ant, r->sig_len, r->sig_mode);
    ets_printf(",%d,%d,\"[%d", r->len, r->first_word_invalid,
               (int)(int16_t)(r->gain * r->buf[0]));   /* ROM ets_printf 不认 %hu，必须 %d */
    for (int i = 1; i < r->len; i++) {
        ets_printf(",%d", (int)(int16_t)(r->gain * r->buf[i]));
    }
    ets_printf("]\"\n");
}

static void writer_task(void *arg)
{
    for (;;) {
        size_t sz = 0;
        csi_rec_t *r = xRingbufferReceive(s_ring, &sz, pdMS_TO_TICKS(100));
        if (!r) {
            continue;
        }
        s_rx_cnt++;
        if (s_csv_on) {
            if (!s_header_printed) {
                ets_printf("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,"
                           "aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,"
                           "secondary_channel,local_timestamp,ant,sig_len,rx_format,len,first_word,data\n");
                s_header_printed = true;
            }
            print_csv_line(r);
        }
        rx_mqtt_accumulate(r->buf, r->len, r->gain);
        vRingbufferReturnItem(s_ring, r);
    }
}

/* ---------------- WiFi / ESP-NOW / CSI ---------------- */

static volatile int s_channel = CONFIG_RX_DEFAULT_CHANNEL;   /* Kconfig 兜底，NVS 命中时覆盖 */

/* ---------- NVS 持久化（信道） ---------- */

static void nvs_load_channel(void)
{
    nvs_handle_t h;
    if (nvs_open("rxcfg", NVS_READONLY, &h) == ESP_OK) {
        int32_t v = 0;
        if (nvs_get_i32(h, "channel", &v) == ESP_OK && v >= 1 && v <= 13) {
            s_channel = (int)v;
            return;                      /* 命中 NVS，覆盖 Kconfig 默认 */
        }
        nvs_close(h);
    }
}

static void nvs_save_channel(int ch)
{
    nvs_handle_t h;
    if (nvs_open("rxcfg", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_i32(h, "channel", ch);
        nvs_commit(h);
        nvs_close(h);
    }
}

static void wifi_init_fixed_channel(void)
{
    nvs_load_channel();                        /* NVS 优先，Kconfig 默认兜底 */
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, s_tx_mac));   /* 对齐 esp-radar：start 前设 MAC */
    esp_netif_create_default_wifi_sta();
    wifi_protocols_t protocols = { .ghz_2g = WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, RX_BW));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(s_channel, RX_SECOND));
}

static void espnow_init(void)
{
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    esp_now_peer_info_t peer = {
#if CONFIG_RX_MQTT_ENABLE
        .channel   = 0,   /* MQTT 模式：跟随路由器信道（STA 关联后自动锁定） */
#else
        .channel   = CONFIG_RX_DEFAULT_CHANNEL,
#endif
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate_config = {
        .phymode = RX_PHY_MODE,
        .rate    = WIFI_PHY_RATE_MCS0_LGI,
        .ersu    = false,
        .dcm     = false,
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
}

#define CHK(tag, call) do { \
        esp_err_t _e = (call); \
        ESP_LOGI(TAG, "%s -> %s (0x%x)", tag, esp_err_to_name(_e), _e); \
    } while (0)

static void wifi_csi_init(void)
{
    /* ESP32-S3 默认配置（与 csi_recv 一致） */
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = false,
    };
    CHK("set_promiscuous", esp_wifi_set_promiscuous(true));
    CHK("set_csi_config", esp_wifi_set_csi_config(&csi_config));
    CHK("set_csi_rx_cb", esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    CHK("set_csi(true)", esp_wifi_set_csi(true));
}

/* ---------------- 控制台 ---------------- */

static int cmd_csv(int argc, char **argv)
{
    s_csv_on = !s_csv_on;
    printf("csv %s\n", s_csv_on ? "on" : "off");
    return 0;
}

static struct {
    struct arg_int *ch;
    struct arg_end *end;
} chan_args;

static int cmd_channel(int argc, char **argv)
{
#if CONFIG_RX_MQTT_ENABLE
    printf("MQTT mode: channel follows the router, cannot set manually\n");
    return 1;
#else
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
    ESP_ERROR_CHECK(esp_wifi_set_channel(ch, RX_SECOND));
    s_channel = ch;
    nvs_save_channel(ch);
    printf("channel -> %d (saved)\n", ch);
    return 0;
#endif
}

static int cmd_scan(int argc, char **argv)
{
    printf("scanning ~3s (CSI paused)...\n");
    const wifi_scan_config_t cfg = {
        .show_hidden = false,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
        .scan_time.active = { .min = 0, .max = 120 },
    };
    esp_err_t err = esp_wifi_scan_start(&cfg, true);      /* 阻塞扫全频段 */
    if (err != ESP_OK) {
        printf("scan failed: %s\n", esp_err_to_name(err));
        return 1;
    }
    uint16_t n = 0;
    esp_wifi_scan_get_ap_num(&n);
    if (n > 20) n = 20;
    wifi_ap_record_t *recs = calloc(n ? n : 1, sizeof(wifi_ap_record_t));   /* 堆分配防栈溢出 */
    if (!recs) { printf("oom\n"); return 1; }
    if (n) ESP_ERROR_CHECK(esp_wifi_scan_get_ap_records(&n, recs));

    double occ[14] = { 0 };
    printf("APs found: %d\n", (int)n);
    for (int i = 0; i < n; i++) {
        printf("  ch%2d  %4d dBm  %s\n", recs[i].primary, recs[i].rssi,
               (const char *)recs[i].ssid);
        double lin = pow(10.0, recs[i].rssi / 10.0) * 1e6;  /* 线性功率×1e6 便于阅读 */
        for (int ch = 1; ch <= 13; ch++)                  /* 20MHz≈4 信道宽 */
            if (abs((int)recs[i].primary - ch) <= 4) occ[ch] += lin;
    }
    free(recs);
    esp_wifi_set_channel(s_channel, RX_SECOND);           /* 扫完全频段后回工作信道 */
    printf("channel occupancy score (lower=better, current=%d):\n", s_channel);
    for (int ch = 1; ch <= 13; ch++)
        printf("  ch%2d: %9.1f%s\n", ch, occ[ch], ch == s_channel ? "   <- current" : "");
    return 0;
}

static int cmd_stats(int argc, char **argv)
{
    printf("rx=%" PRIu32 " drop=%" PRIu32 " csv=%d channel=%d bw=%s "
           "ring_free=%u\n",
           s_rx_cnt, s_drop_cnt, s_csv_on, s_channel,
           RX_BW == WIFI_BW_HT40 ? "HT40" : "HT20",
           (unsigned)xRingbufferGetCurFreeSize(s_ring));
    return 0;
}

static void console_init(void)
{
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_cfg = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    esp_console_dev_uart_config_t uart_cfg = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    repl_cfg.prompt = "rx>";
    repl_cfg.max_cmdline_length = 64;
    ESP_ERROR_CHECK(esp_console_new_repl_uart(&uart_cfg, &repl_cfg, &repl));

    const esp_console_cmd_t csv_cmd = {
        .command = "csv", .help = "toggle CSI CSV output",
        .hint = NULL, .func = &cmd_csv,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&csv_cmd));

    chan_args.ch = arg_int1(NULL, NULL, "<ch>", "channel 1..13 (fixed-channel mode only)");
    chan_args.end = arg_end(1);
    const esp_console_cmd_t chan_cmd = {
        .command = "channel", .help = "set wifi channel (must match TX)",
        .hint = NULL, .func = &cmd_channel, .argtable = &chan_args
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&chan_cmd));

    const esp_console_cmd_t stats_cmd = {
        .command = "stats", .help = "rx/drop counters and config",
        .hint = NULL, .func = &cmd_stats,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&stats_cmd));

    const esp_console_cmd_t scan_cmd = {
        .command = "scan", .help = "scan APs and print per-channel occupancy (CSI pauses ~3s)",
        .hint = NULL, .func = &cmd_scan,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&scan_cmd));

    replay_console_register();

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

    s_ring = xRingbufferCreate(CONFIG_RX_RING_BUF_ITEMS * sizeof(csi_rec_t),
                               RINGBUF_TYPE_NOSPLIT);

#if CONFIG_RX_MQTT_ENABLE
    /* MQTT 模式：用 fixed_channel 路径初始化 WiFi（已验证可靠），再叠加 STA 连接。
       路由器信道与 NVS 保存的信道必须一致（ch8），TX 也在同信道。 */
    wifi_init_fixed_channel();
    {
        wifi_config_t wc = { 0 };
        strlcpy((char *)wc.sta.ssid, CONFIG_RX_WIFI_SSID, sizeof(wc.sta.ssid));
        strlcpy((char *)wc.sta.password, CONFIG_RX_WIFI_PASSWORD, sizeof(wc.sta.password));
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
        ESP_ERROR_CHECK(esp_wifi_connect());    /* 在当前信道上关联路由器 */
        ESP_LOGI(TAG, "STA connecting to %s on ch%d...", CONFIG_RX_WIFI_SSID, s_channel);
    }
#else
    wifi_init_fixed_channel();
#endif
    espnow_init();
    wifi_csi_init();

    ESP_LOGI(TAG, "==== RX collector ====");
    ESP_LOGI(TAG, "csv=%d bw=%s ring_items=%d", s_csv_on,
             RX_BW == WIFI_BW_HT40 ? "HT40" : "HT20", CONFIG_RX_RING_BUF_ITEMS);

#if CONFIG_RX_MQTT_ENABLE
    rx_mqtt_start();
#endif

    console_init();
    xTaskCreate(writer_task, "writer", 8192, NULL, 5, NULL);
}
