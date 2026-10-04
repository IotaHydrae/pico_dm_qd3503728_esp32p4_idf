# 引脚定案：QD3503728 扩展板接到 ESP32-P4

> 25 个信号里 **23 个直接落在空闲 GPIO**，另 2 个落在排针的 `usbd1_n`/`usbd1_p`
> —— 那两根就是 **GPIO24/25**，只是**默认**归 USB 功能，显式切回普通 GPIO 即可
> ⇒ **扩展板可以直接插，不需要飞线** ✓。

## TL;DR

- 定案表见下（已落成代码：`main/include/board_pins.h`）。**GPIO46–52 已确认空闲** ✓
  （C6↔P4 的 SDIO 不走这几个脚）。
- 唯一要显式处理的是 **GPIO24/25**：P4 datasheet 6.3 注 "By default, the USB function
  is enabled for USB pins (i.e., GPIO24/26 and GPIO25/27)"，且这两个脚默认驱动能力
  40 mA（其它脚 20 mA）⇒ 初始化时切回普通 GPIO，并别启用对应 USB 实例。
- 烧写/日志走**板上 USB 转串口（UART0）**，与这两个脚无关 ✓。
- 触模 FT6236 在 I2C **GPIO23/22**；调试串口位 **48/47**（本项目不用）。

## 定案表

| 信号 | P4 GPIO | 信号 | P4 GPIO |
|---|---|---|---|
| D0–D3 | 52, 51, 31, 30 | D12–D13 | 8, 7 |
| D4–D7 | 29, 28, 50, 49 | D14–D15 | **24, 25**（原 USB1 脚，须切回 GPIO）|
| D8–D11 | 5, 4, 3, 2 | CS / WR / RS | 46 / 33 / 32 |
| RES | 26 | BLK | 21 |
| 触模 SDA / SCL | 23 / 22 | 调试 TX / RX | 48 / 47 |

## 翻译链（方法可复用：三级翻译，不能靠"看起来像"）

```text
扩展板信号名（PUD：lib/pico-display-lib/configs/pico_dm_qd3503728.cmake）
  → Pico GPIO 号（RP2040/RP2350）
  → Pico 物理脚号 1..40（标准 40-pin 布局）
  → 本板同一物理脚位置（用户给的板子丝印，顶视图）
  → P4 GPIO 号
```

- **两板之间的不变量是"物理脚位置"**，不是 GPIO 号；直接按 GPIO 号对齐必错。
- 每级都要留证据：第 1 级来自已跑通的 PUD 配置 ✓，第 3 级来自板子丝印 ✓。
- 顶视图/底视图必须先确认，否则整列镜像。
- 交叉验证：参考工程（微雪 `ESP32-P4-Platform`）的 BSP 引脚定义可用来核对本板哪些脚
  被板载外设占用（C6 的 SDIO、microSD、USB-C 等）。

## 边界与陷阱

- **GPIO24/25 不是不能用**，但默认归 USB，且默认驱动能力（40 mA）与其它脚不同 ⇒ 做
  并行总线时这两根的边沿更快、偏斜更大；本项目把 19 根线统一到同一档
  （`GPIO_DRIVE_CAP_1`，≈10 mA）后才干净。
- **先确认引脚占用，再接面板总线**：S3 那次顺序反了，面板 D8-D12 压在 octal PSRAM 的
  GPIO33-37 上，8 MB PSRAM 报废。P4 的 PSRAM 在封装内、与排针无冲突 ✓，但流程仍要守。
- 逐脚核对用的资料在 `../hardware-docs/`（厂商数据手册与 TRM，**上游内容，未改动**）。
  从矢量 PDF 抽网标的办法（`pdftotext -bbox` + 按坐标聚类重建排针表）在需要重做审计时
  仍然有用，但中间过程不必留档。

## 相关

- [bring-up.md](bring-up.md)、[panel-ili9488-i80.md](panel-ili9488-i80.md)
