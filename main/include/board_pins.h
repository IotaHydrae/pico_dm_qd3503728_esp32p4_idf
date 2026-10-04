/*
 * 显示扩展板（QD3503728 / ILI9488，8080 16-bit 并口）在微雪 ESP32-P4-WIFI6
 * 上的引脚定义。
 *
 * 来源链（详见 notes/pin-map.md）：
 *   扩展板信号定义 → Pico GPIO → Pico 物理脚(1..40) → 本板同一物理脚 → P4 GPIO
 * 扩展板那一侧的定义在 Pico-USB-Display 的
 *   lib/pico-display-lib/configs/pico_dm_qd3503728.cmake
 * （那边是 RP2040/RP2350 的 GPIO 号：数据总线 GP0-GP15 连续 16 根，
 *   CS=GP18, WR=GP19, RS=GP20, RES=GP22, BLK=GP28, 触模 I2C SDA/SCL=GP26/27）。
 *
 * 翻译结果（用户给的板子丝印，顶视图）：**扩展板可以直接插，不需要飞线** ✓
 * 25 个信号里 23 个直接落在空闲 GPIO 上；剩下两个落在排针的 usbd1_n / usbd1_p
 * 位置，那两个脚就是 GPIO24 / GPIO25。
 *
 * ⚠️ GPIO24/25 的注意点：P4 datasheet 6.3 节 —— "By default, the USB function
 * is enabled for USB pins (i.e., GPIO24/26 and GPIO25/27)"。它们是普通 IO_MUX
 * 引脚，但**默认**归 USB 功能，所以初始化时必须显式把它们切回普通 GPIO，
 * 且不要启用对应的 USB FS 驱动（我们的烧写与日志走板上 USB 串口/UART0，
 * 不用这两个脚）。
 */
#ifndef __PUD_BOARD_PINS_H
#define __PUD_BOARD_PINS_H

/* 8080 数据总线 D0..D15（对应扩展板的 DB0..DB15） */
#define TFT_PIN_D0    52
#define TFT_PIN_D1    51
#define TFT_PIN_D2    31
#define TFT_PIN_D3    30
#define TFT_PIN_D4    29
#define TFT_PIN_D5    28
#define TFT_PIN_D6    50
#define TFT_PIN_D7    49
#define TFT_PIN_D8     5
#define TFT_PIN_D9     4
#define TFT_PIN_D10    3
#define TFT_PIN_D11    2
#define TFT_PIN_D12    8
#define TFT_PIN_D13    7
#define TFT_PIN_D14   24 /* 排针上的 usbd1_n */
#define TFT_PIN_D15   25 /* 排针上的 usbd1_p */

/* 控制线 */
#define TFT_PIN_CS    46
#define TFT_PIN_WR    33
#define TFT_PIN_RS    32 /* 扩展板的 TFT_PIN_RS，即 DC */
#define TFT_PIN_RES   26
#define TFT_PIN_BLK   21

/* 触模 FT6236（扩展板的 I2C；P4 侧还剩 48/47 是原来的调试串口位，
 * 本项目不用） */
#define TP_PIN_SDA    23
#define TP_PIN_SCL    22

/* 面板参数（与扩展板 config 一致） */
#define TFT_HOR_RES     320 /* 原生（未旋转）分辨率：面板自己的 x/y 方向 */
#define TFT_VER_RES     480
#define TFT_ROTATION    1
/* 总线写时钟的**请求值**（也是上报给主机的 pixelclock_khz 语义）。
 * 注意实际值被 i80 的整数分频取整到 80/n MHz（见 panel.c 与
 * notes/general/esp-idf-i80-pclk-divider.md）：40 MHz 是实测可用的最高档，
 * 请求 50 MHz 会实际跑 80 MHz 且显示不正常 ✗ ⇒ 这里必须写 40 MHz。 */
#define TFT_BUS_CLK_KHZ 40000

/* 逻辑（旋转之后）分辨率 —— 显示层对外、decoder 与主机协议看到的坐标系。
 * rot 1/3 时行列互换，就是"横屏 480x320"。算法与 ESP32-S3 版 pud.c 里
 * `if (TFT_ROTATION & 1) { xres = TFT_VER_RES; ... }` 完全一致，所以主机侧
 * 与 decoder 层不用改。 */
#if (TFT_ROTATION & 1)
#define TFT_LOG_HOR_RES TFT_VER_RES
#define TFT_LOG_VER_RES TFT_HOR_RES
#else
#define TFT_LOG_HOR_RES TFT_HOR_RES
#define TFT_LOG_VER_RES TFT_VER_RES
#endif
/* 逻辑坐标下的总字节数（RGB565） */
#define TFT_LOG_FRAME_BYTES (TFT_LOG_HOR_RES * TFT_LOG_VER_RES * 2)

#endif /* __PUD_BOARD_PINS_H */
