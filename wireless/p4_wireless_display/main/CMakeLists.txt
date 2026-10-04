idf_component_register(
    # panel.c 是从同仓固件（仓库根 main/）照抄的显示层（已上板验收）。
    # 改动必须两边同步：diff main/panel.c ../../main/panel.c
    SRCS "main.c" "panel.c"
    INCLUDE_DIRS "."
    REQUIRES esp_lcd esp_driver_jpeg esp_driver_ledc esp_driver_gpio esp_wifi esp_netif esp_event nvs_flash lwip esp_timer
)
