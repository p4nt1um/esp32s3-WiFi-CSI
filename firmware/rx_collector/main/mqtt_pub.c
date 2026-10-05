/*
 * MQTT 上行模块（Kconfig: RX_MQTT_ENABLE，默认关闭）。
 * M1 范围：STA 连接路由器 + 每秒上报一次「1Hz 平均幅度数组」与统计 JSON。
 *   - <prefix>/amp : {"t":ms,"n":cnt,"amp":[52 个 %.1f]}   ← 供服务器存在性/链路监控
 *   - <prefix>/stat: {"t":ms,"rx":N,"drop":M}
 * 完整 10Hz 特征流与呼吸结果上行为 M2/M3 范围（见方案 §2 上行数据策略）。
 * 注意：此模块需硬件联调后才能验证，默认不编译进固件。
 */
#include "sdkconfig.h"     /* 必须显式引入： mqtt_pub.c 的条件编译依赖 CONFIG_RX_MQTT_ENABLE */
#include "mqtt_pub.h"

#if !CONFIG_RX_MQTT_ENABLE

/* 关闭时的空实现 */
void rx_mqtt_accumulate(const int8_t *iq, uint16_t len, float gain) { (void)iq; (void)len; (void)gain; }
void rx_mqtt_start(void) {}
void wifi_init_sta(void) {}

#else /* CONFIG_RX_MQTT_ENABLE */

#include <stdio.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "mqtt_client.h"

#define AMP_MAX_SUB 64
static const char *TAG = "mqtt_pub";

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static float s_sum[AMP_MAX_SUB];
static uint32_t s_cnt;
static uint64_t s_boot_ms(void)
{
    return (uint64_t)(esp_timer_get_time() / 1000); /* esp_timer via FreeRTOS tick fallback */
}

/* 由 writer_task 调用（任务上下文，非 ISR） */
void rx_mqtt_accumulate(const int8_t *iq, uint16_t len, float gain)
{
    if (len < 2 || len / 2 > AMP_MAX_SUB) {
        return;
    }
    portENTER_CRITICAL(&s_mux);
    for (int k = 0; k + 1 < len; k += 2) {
        float i = gain * iq[k];
        float q = gain * iq[k + 1];
        s_sum[k / 2] += sqrtf(i * i + q * q);
    }
    s_cnt++;
    portEXIT_CRITICAL(&s_mux);
}

static esp_mqtt_client_handle_t s_client;

static void mqtt_task(void *arg)
{
    char topic[64];
    /* 批量发布：每 1 秒发 1 条含 10 个时间点的消息。payload 用堆分配（8KB 太大不能放栈） */
    #define PAYLOAD_SIZE 8192
    char *payload = malloc(PAYLOAD_SIZE);
    char *stat_buf = malloc(128);
    if (!payload || !stat_buf) {
        ESP_LOGE(TAG, "mqtt_task: payload malloc failed");
        free(payload); free(stat_buf);
        vTaskDelete(NULL);
        return;
    }
    int stat_div = 0;
    int publish_fail = 0;
    const int WATCHDOG_LIMIT = 30;          /* 30 次×1s=30s → 重启 */
    int batch_count = 0;
    int batch_off = 0;
    

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(100));      /* 10Hz 采集 */

        if (!s_client) {
            continue;
        }

        float snap[AMP_MAX_SUB] = {0};
        uint32_t n;
        portENTER_CRITICAL(&s_mux);
        n = s_cnt;
        if (n) {
            for (int k = 0; k < AMP_MAX_SUB; k++) {
                snap[k] = s_sum[k] / n;
                s_sum[k] = 0;
            }
            s_cnt = 0;
        }
        portEXIT_CRITICAL(&s_mux);

        int csi_active = (n > 0);
        uint16_t nsub = 52;

        /* 累积到批量缓冲 */
        if (batch_count == 0) {
            batch_off = snprintf(payload, PAYLOAD_SIZE, "{\"t0\":%llu,\"samples\":[",
                                 (unsigned long long)s_boot_ms());
            
        }
        batch_off += snprintf(payload + batch_off, PAYLOAD_SIZE - batch_off,
                              "%s[", batch_count ? "," : "");
        for (int k = 0; k < nsub && k < AMP_MAX_SUB; k++) {
            batch_off += snprintf(payload + batch_off, PAYLOAD_SIZE - batch_off,
                                  "%s%.1f", k ? "," : "", snap[k]);
        }
        batch_off += snprintf(payload + batch_off, PAYLOAD_SIZE - batch_off, "]");
        batch_count++;

        /* 每 10 个样本（1 秒）发布一次 */
        if (batch_count >= 10) {
            snprintf(payload + batch_off, PAYLOAD_SIZE - batch_off, "]}");
            snprintf(topic, sizeof(topic), "%s/amp", CONFIG_RX_MQTT_TOPIC_PREFIX);
            int rc = esp_mqtt_client_publish(s_client, topic, payload, 0, 0, 0);

            /* 看门狗：有 CSI 但发布失败 */
            if (csi_active && rc < 0) {
                if (++publish_fail >= WATCHDOG_LIMIT) {
                    ESP_LOGE(TAG, "MQTT watchdog: reboot");
                    esp_restart();
                }
            } else {
                publish_fail = 0;
            }
            batch_count = 0;
        }

        if (++stat_div >= 100) {
            stat_div = 0;
            snprintf(topic, sizeof(topic), "%s/stat", CONFIG_RX_MQTT_TOPIC_PREFIX);
            snprintf(payload, PAYLOAD_SIZE, "{\"t\":%llu}", (unsigned long long)s_boot_ms());
            esp_mqtt_client_publish(s_client, topic, payload, 0, 0, 0);
        }
    }
}

static void event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        esp_wifi_connect();    /* 简单重连 */
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        uint8_t ch = 0;
        wifi_second_chan_t sec = WIFI_SECOND_CHAN_NONE;
        esp_wifi_get_channel(&ch, &sec);
        ESP_LOGI(TAG, "got ip, router channel=%d (TX must be on the same channel!)", ch);
    } else if (id == (int32_t)MQTT_EVENT_CONNECTED) {   /* 只比 id：handler 经 esp_mqtt_client_register_event 注册 */
        ESP_LOGI(TAG, "mqtt connected");
    }
}

void wifi_init_sta(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    wifi_protocols_t protocols = { .ghz_2g = WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    ESP_ERROR_CHECK(esp_wifi_start());
    /* 带宽/省电必须在 wifi_init_sta 内设置（app_main 里的上下文调 NOT_INIT） */
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, WIFI_BW_HT20));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    wifi_config_t wc = { 0 };
    strlcpy((char *)wc.sta.ssid, CONFIG_RX_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, CONFIG_RX_WIFI_PASSWORD, sizeof(wc.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, event_handler, NULL));
}

void rx_mqtt_start(void)
{
    const esp_mqtt_client_config_t mc = {
        .broker.address.uri = CONFIG_RX_MQTT_URI,
        .buffer.size = 8192,             /* 批量 amp 消息 ~3.5KB，默认 1024 不够 */
        .buffer.out_size = 8192,
    };
    s_client = esp_mqtt_client_init(&mc);
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, event_handler, NULL));
    ESP_ERROR_CHECK(esp_mqtt_client_start(s_client));
    xTaskCreate(mqtt_task, "mqtt_pub", 12288, NULL, 4, NULL);  /* 12KB：批量 snprintf 需要余量 */
}

#endif /* CONFIG_RX_MQTT_ENABLE */
