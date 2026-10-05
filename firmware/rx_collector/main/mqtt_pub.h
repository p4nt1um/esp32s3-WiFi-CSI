#pragma once
#include <stdint.h>
#include <stddef.h>

/* 写任务每条 CSI 记录调用一次：累积增益补偿后的子载波幅度（供 MQTT 周期上报） */
void rx_mqtt_accumulate(const int8_t *iq, uint16_t len, float gain);

/* MQTT 模式：STA 连接路由器 + 启动 MQTT 客户端与 1Hz 上报任务（Kconfig 门控） */
void rx_mqtt_start(void);

/* MQTT 模式下的 WiFi STA 初始化（信道跟随路由器，TX 必须同信道） */
void wifi_init_sta(void);
