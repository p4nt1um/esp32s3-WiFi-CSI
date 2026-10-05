idf_component_register(SRCS "app_main.c" "mqtt_pub.c" "replay.c" INCLUDE_DIRS "."
                       PRIV_REQUIRES csi_core driver console nvs_flash esp_wifi esp_netif esp_event esp_timer mqtt)
