# pico_dm_qd3503728_esp32p4_idf

PUD（Pico-USB-Display）设备端固件的 **ESP32-P4 移植**。同一个 PUD 主机协议、
同一块 480x320 面板，但换到有**硬件 JPEG 解码**和**高速 USB**的平台上 ——
ESP32-S3 那次"CPU 软解 + 全速 USB"的前提在这里变了。

## 现在是什么状态

**显示与解码这条链已经打通并量化完毕**，还差 **USB 主机链路 + PUD 协议层**：

| 项 | 状态 |
|---|---|
| 面板 | ILI9488 16-bit i80（IDF `esp_lcd`，不用 LovyanGFX）：点亮/颜色/**旋转 1（横屏 480x320）**都上板确认 ✓ |
| 总线 | 定档 **40 MHz**（实际 40 MHz），整帧一笔 **77.6 MB/s**（理论 80 的 97%）✓ |
| 解码 | **硬件 JPEG**（`esp_driver_jpeg`）：480x320→RGB565 **1.76 ms/帧**（≈570 fps），零拷贝直喂 i80 ✓ |
| 端到端 | 解码+上屏 **5.72 ms/帧（174.7 fps）** ⇒ 对 60 fps 有 3 倍余量 ✓ |
| 未做 | USB 设备栈/PUD 协议层（P4 原生 USB 在 P1 的 MX1.25 4pin 上，等线）、触模 |

`idf.py flash` 后屏幕上会**三相位各 8 秒轮转**：方向测试图 → 棋盘 →
硬件解码出来的桌面截图（都在连续重画，示波器随时可量 WR）。

**接手先读 [`HANDOFF.md`](HANDOFF.md)**；实测判据与踩过的坑在
[`notes/`](notes/README.md)（索引）；约定与硬约束在 [`AGENTS.md`](AGENTS.md)。

## 工具链

**ESP32-P4 需要 ESP-IDF 5.3 以上**（5.2 的 `idf.py --list-targets` 里没有
esp32p4，`soc/esp32p4` 目录存在也不代表能编译）。本机：
- `~/esp/esp-idf`（release/v5.2）：**保留给 ESP32-S3 那个项目**，别动；
- `~/esp/esp-idf-v6.1`：本项目的工具链，riscv32-esp-elf（P4 是 RISC-V）。

```bash
source ~/esp/esp-idf-v6.1/export.sh
idf.py build
idf.py -p <PORT> flash monitor      # 板上 USB 转串口接了 DTR/RTS，不用按 BOOT
```

`sdkconfig.defaults` 里有两个**必设**项（芯片 rev v1.0 的坑，见 HANDOFF §5）。

## 继承自 ESP32-S3 项目的东西

- 显示层接口契约（`tft_video_flush` / `tft_async_video_flush` / `tft_async_video_wait`），
  decoder 层将来可以原样继承；
- 主机侧协议与调试方法论（面包屑、证据分级、四行前置门、一轮一个变量）；
- `scripts/flash-recover.sh` 与 `main/boot_request.c`（**本板用不上**：DTR/RTS 自动
  复位就能烧；`boot_request.c` 依赖的寄存器 P4 也没有，暂不参与构建）。

## 显示层：自己实现，不用 LovyanGFX

面板驱动写在我们自己手里（建在 IDF 官方 `esp_lcd` 之上），只负责引脚/总线、
背光、旋转、以及"把一块像素刷上去"。为什么换掉 LovyanGFX、以及必须遵守的契约，
见 `AGENTS.md` §5。

## 约定

见 `AGENTS.md`。最简单的一条：**未经明确指示不要 commit / push**。
