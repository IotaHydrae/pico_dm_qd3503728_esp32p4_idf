idf_component_register(
    # boot_request.c 暂不参与构建：它用 RTC_CNTL_FORCE_DOWNLOAD_BOOT 让应用自己
    # 进 ROM 下载态，而 P4（RISC-V）没有这个寄存器定义 ✗。好消息是本板**不需要**
    # 它：板上 USB 转串口的 DTR/RTS 自动复位，实测烧写不用按 BOOT ✓（见
    # notes/bring-up.md）。哪天我们接管了 P4 的原生 USB，再照 P4 的方式实现它。
    SRCS "main.c" "panel.c" "jpeg_bench.c"
    # assets/ 直接放在 main/ 下，源码里用 "assets/jpeg_assets.h" 引（GCC 会先找
    # 包含它的那个文件所在目录）。
    INCLUDE_DIRS "include"
    # esp_driver_jpeg = P4 的硬件 JPEG 编解码驱动（IDF 6 把驱动拆成独立组件了）
    REQUIRES esp_lcd esp_driver_ledc esp_driver_gpio esp_driver_jpeg esp_timer
)
