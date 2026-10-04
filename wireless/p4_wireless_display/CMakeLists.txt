# 无线显示器（P4 + 片内 C6 走 WiFi）：最小端到端 demo。
#
# 与同仓固件（仓库根的 main/）的关系：显示层（panel.c / tft.h / board_pins.h）
# 是从那边**照抄**过来的（那一版已经上板验收：40 MHz、77.6 MB/s、旋转 1、硬件 JPEG
# 零拷贝上屏）。抄而不是引用，是为了让两边互不影响；**改动必须两边同步**
# （用 `diff main/panel.c ../../main/panel.c` 核对）。
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(p4_wireless_display)
