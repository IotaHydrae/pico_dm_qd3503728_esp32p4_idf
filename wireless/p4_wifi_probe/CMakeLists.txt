# P4 + C6 (ESP-Hosted) 链路吞吐探针 —— 独立的 IDF 工程（与固件工程互不依赖，
# 现同仓放在 wireless/ 下），目的是先证明"链路可用 + 能跑多快"，不接显示。
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(p4_wifi_probe)
