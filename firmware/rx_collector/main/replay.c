/*
 * CSV 回放模式（M2a）：从 UART1 读入 esp-csi 格式 CSV 行，喂给与 PC 对拍同一份
 * csi_core 算法核心，把逐窗结果打到 UART0 控制台。
 * 用途回归测试：换参数/换固件后回放同一份数据，验证结果不劣化。
 * 使用：控制台输入 replay → PC 向 UART1 发送 CSV（921600）→ 以单独一行 END 结束。
 */
#include "replay.h"

#if CONFIG_RX_REPLAY_ENABLE

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/uart.h"
#include "esp_console.h"
#include "esp_heap_caps.h"
#include "esp_log.h"

#include "csi_core.h"

#define REPLAY_UART   UART_NUM_1
#define LINE_MAX      4096

static const char *TAG = "replay";

static int16_t *s_iq;
static size_t s_cnt, s_cap_stored;
static int s_nsub;

static int feed_line(const char *line)
{
    const char *p = strstr(line, ",\"[");
    if (!p) return -1;
    if (s_nsub == 0) {
        int n = 0;
        for (const char *r = p + 3; *r && *r != ']'; r++)
            if (*r == ',') n++;
        s_nsub = (n + 1) / 2;
        if (s_nsub <= 0 || s_nsub > CSI_CORE_MAX_SUB) return -1;
    }
    p += 3;
    const size_t row = (size_t)s_nsub * 2;
    while (*p && *p != ']') {
        char *end;
        long v = strtol(p, &end, 10);
        if (end == p) break;
        if (s_cnt == s_cap_stored) {
            ESP_LOGW(TAG, "replay buffer full (%u samples)", (unsigned)s_cnt);
            return 1;                       /* 停止接收，直接处理已有数据 */
        }
        s_iq[s_cnt++] = (int16_t)v;
        p = end;
        if (*p == ',') p++;
    }
    return (s_cnt % row == 0) ? 0 : -2;
}

static void replay_task(void *arg)
{
    const int max_samples = CONFIG_RX_REPLAY_MAX_SAMPLES;
    s_iq = heap_caps_malloc((size_t)max_samples * CSI_CORE_MAX_SUB * 2 * sizeof(int16_t),
                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_iq)
        s_iq = heap_caps_malloc((size_t)max_samples * CSI_CORE_MAX_SUB * 2 * sizeof(int16_t),
                                MALLOC_CAP_8BIT);
    if (!s_iq) {
        ESP_LOGE(TAG, "no memory for replay buffer");
        vTaskDelete(NULL);
        return;
    }
    s_cap_stored = (size_t)max_samples * CSI_CORE_MAX_SUB * 2;
    s_cnt = 0; s_nsub = 0;

    const uart_config_t uc = {
        .baud_rate = CONFIG_RX_REPLAY_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    uart_driver_install(REPLAY_UART, 16384, 0, 0, NULL, 0);
    uart_param_config(REPLAY_UART, &uc);
    uart_set_pin(REPLAY_UART, UART_PIN_NO_CHANGE, CONFIG_RX_REPLAY_UART_RX_PIN,
                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);

    printf("REPLAY READY (uart1 rx gpio=%d baud=%d) send CSV then END\n",
           CONFIG_RX_REPLAY_UART_RX_PIN, CONFIG_RX_REPLAY_UART_BAUD);

    char line[LINE_MAX];
    int li = 0;
    for (;;) {
        uint8_t ch;
        int n = uart_read_bytes(REPLAY_UART, &ch, 1, pdMS_TO_TICKS(100));
        if (n != 1) continue;
        if (ch == '\n' || ch == '\r') {
            if (li == 0) continue;
            line[li] = 0;
            li = 0;
            if (strncmp(line, "END", 3) == 0) break;
            if (strncmp(line, "CSI_DATA,", 9) == 0) {
                if (feed_line(line) == 1) break;   /* 缓冲满 */
            }
            continue;
        }
        if (li < LINE_MAX - 1) line[li++] = (char)ch;
    }

    int samples = (int)(s_cnt / (size_t)(s_nsub ? s_nsub * 2 : 1));
    if (s_nsub && samples >= 64) {
        csi_core_input in = { .n_samples = samples, .n_sub = s_nsub, .iq = s_iq };
        int max_win = samples / 10 - 30 + 2;
        csi_core_result res = { 0 };
        res.win = heap_caps_malloc(sizeof(csi_core_window) * (size_t)max_win,
                                   MALLOC_CAP_8BIT);
        if (res.win) {
            int rc = csi_core_run(&in, 100.0, &res);
            if (rc == 0) {
                printf("REPLAY RESULT t,bpm_fft,bpm_ac,agree,peak_ratio,power,valid,bpm\n");
                for (int w = 0; w < res.n_win; w++) {
                    const csi_core_window *r = &res.win[w];
                    if (r->valid)
                        printf("%.6g,%.6g,%.6g,%d,%.6g,%.6g,1,%.6g\n", r->t, r->bpm_fft,
                               r->bpm_ac, r->agree, r->peak_ratio, r->power, r->bpm);
                    else
                        printf("%.6g,%.6g,%.6g,%d,%.6g,%.6g,0,nan\n", r->t, r->bpm_fft,
                               r->bpm_ac, r->agree, r->peak_ratio, r->power);
                }
                printf("REPLAY DONE windows=%d samples=%d sub=%d\n", res.n_win, samples, s_nsub);
            } else {
                printf("REPLAY ERROR csi_core_run=%d\n", rc);
            }
            free(res.win);
        }
    } else {
        printf("REPLAY DONE no-data (sub=%d samples=%d)\n", s_nsub, samples);
    }

    free(s_iq); s_iq = NULL;
    uart_driver_delete(REPLAY_UART);
    vTaskDelete(NULL);
}

static int cmd_replay(int argc, char **argv)
{
    printf("starting replay task...\n");
    xTaskCreate(replay_task, "replay", 8192, NULL, 5, NULL);
    return 0;
}

void replay_console_register(void)
{
    const esp_console_cmd_t cmd = {
        .command = "replay",
        .help = "replay CSV from UART1 through csi_core and print windows",
        .hint = NULL, .func = &cmd_replay,
    };
    esp_console_cmd_register(&cmd);
}

#else /* !CONFIG_RX_REPLAY_ENABLE */

void replay_console_register(void) {}

#endif
