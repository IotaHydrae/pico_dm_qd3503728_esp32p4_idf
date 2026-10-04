# AGENTS.md — pico_dm_qd3503728_esp32p4_idf

PUD 设备端固件的 **ESP32-P4 移植**。本文件是接手须知。ESP32-S3 那一版的经验在
`../pico_dm_qd3503728_esp32s3_idf/AGENTS.md`（那里的 §2 和 §4 务必先读：
一个讲"内存引脚 vs 面板总线"的硬冲突，一个讲"绝对不要碰的 USB 操作"）。

## 接手前必读（**强制**）

> 动这个仓库的任何代码、配置或文档**之前**，先读工作区 `../skills/` 里的四份 skill
> （摘要随仓放在 [`skills/`](skills/)，逐字节相同；完整版在工作区），并按其中的规则做事。
> 它们**不是参考资料，是强制流程与验收标准**；不符合其中规则的产出视为未完成。

| skill | 一句话 | 本仓最容易踩的 |
|---|---|---|
| Repository Exploration | 先理解再修改；证据优先于直觉 | 未确认就写 `PROBABLY`；拿"通常如此"替代本仓代码事实 |
| Knowledge | 首屏结论、事实分级、信息预算、**漂移检查** | 把研究过程写进知识库；`cat >>` 追加"更新于某日" |
| Testing | `tests/` 解释观察、`tools/` 只出事实；oracle 显式声明 | 拿观测值当期望值；硬件缺失报成 FAIL（应为 ENVIRONMENT_ERROR）|
| Code Quality | **能跑 ≠ 完成**；可读性有硬标准 | 注释写"是什么"；过时注释不删；机制/策略混在一起 |

工作区级约定（四行闸门、全局纪律、优先级）见 [`../AGENTS.md`](../AGENTS.md)。

## 0. 现状

| 项 | 状态 |
|---|---|
| 启动 | 芯片/堆/复位原因 → 面板初始化 → 总线自检 → 硬件 JPEG 基准 → **三相位**（方向测试图 / 棋盘 / 硬件解码的桌面截图）各 8 秒轮转 ✓ |
| 面板 | ILI9488 16-bit i80，走 IDF `esp_lcd`（**不用 LovyanGFX**，见 §5）：点亮 ✓ 颜色 ✓ **旋转 1（横屏 480x320）上板确认正确** ✓ |
| 写时钟 | 定档 **40 MHz**（实际 40 MHz，人眼确认与 20 MHz 一样干净 ✓）。注意 i80 只能整数分频、基数是 80 MHz ⇒ **请求值被向下取整到 80/n MHz**：请求 50 MHz 实际得到 **80 MHz**，而 80 MHz 实测**显示不对（超出面板/信号极限）✗**。详见 `notes/general/esp-idf-i80-pclk-divider.md` 与 `notes/panel-ili9488-i80.md` |
| 吞吐 | 总线（整帧一笔）：40 MHz 下 **77.6 MB/s**（理论 80 的 97%）、20 MHz 下 39.4 MB/s；CPU 填充：**7.3~7.7 ms/帧**（与总线同量级）⇒ 串行只有 18.2 MB/s，**decoder 必须双缓冲 + 异步**。详见 `notes/lcd-transfer-throughput.md` |
| 烧写 | 板上 USB 转串口的 DTR/RTS 接着 EN/BOOT ⇒ `idf.py flash` 即可，**不用按键** ✓ |
| 解码 | **硬件 JPEG**（`esp_driver_jpeg`）：480x320 → RGB565 只要 **1.7~1.8 ms/帧**（≈570 fps），比总线快 2.3 倍 ✓；输入/输出缓冲**必须在 PSRAM**（2D-DMA 限制），而 i80 **能直接读 PSRAM**（零拷贝，与内部 RAM 源同速 77.5 MB/s）✓ |
| 端到端 | 解码 + 上屏：串行 **5.72 ms/帧（174.7 fps）**，对 60 fps 有 3 倍余量 ✓ |
| 未做 | USB 主机链路（P4 原生 USB 在 P1 的 MX1.25 4pin 上，等线）、PUD 协议层、触模 |

细节与判据在各 `notes/*.md`（**索引在 `notes/README.md`**），接手先读 `HANDOFF.md`。

## 1. 仓库约定

- **未经用户明确说"提交"，不要 `git commit` / `git push`**；提交用 `git commit -s`，
  kernel 风格摘要 `子系统: 祈使句`。
- 一轮只做一件事；探针代码提交前必须清干净。
- 不写绝对主机路径 / IP / 私有设备名。
- `notes/` 是知识库，**按工作区的 developer-knowledge skill 维护**（首屏给结论、
  事实分级 ✓/✗、一文档一问题、信息预算 150 行、更新用合并重写、每次改都做**漂移检查**）；
  索引在 `notes/README.md`。通用结论放 `notes/general/`，项目结论放 `notes/` 根。

## 2. 开工前必须先确认的三件事（按顺序）

1. **内存（PSRAM）占了哪些脚**：P4 的 PSRAM 也在封装内且带宽高（200 MHz）。
   先把 PSRAM 的引脚、以及面板/摄像头要用的引脚列清楚，**再**接面板总线。
   ESP32-S3 那次全部痛苦都来自顺序反了：面板 D8-D12 正好压在 octal PSRAM 的
   GPIO33-37 上，开了 PSRAM 就必然无限重启，最后只能放弃 PSRAM（8 MB 白扔）。
2. **烧写路径**：板上 USB 转串口的 DTR/RTS 有没有接到 EN/BOOT？
   接了 ⇒ 一键烧写（不需要手按键）；没接 ⇒ 要么手动按键，要么靠本仓库继承的
   `boot_request.c`（应用自己进下载态）。ESP32-S3 项目实测：**不要**用
   `esptool --before usb_reset`，它会把主机的 xHCI 控制器打死。
3. **面板接口**：P4 有多条路 —— **LCD_CAM i80 / RGB**、MIPI-DSI/CSI、PARLIO
   （`SOC_PARLIO_LCD_SUPPORTED`）。手上这块 QD3503728 是 16-bit i80 ⇒ 走
   **LCD_CAM i80 + IDF `esp_lcd_panel_io_i80`**，总线协议和 S3 一样，但**两个坑
   不一样**：① `LCD_START` 的同步语义一样要靠显式同步点（§5 的契约）；
   ② **DMA 源不限于内部 RAM** —— P4 的 i80 驱动支持 PSRAM 源（实测与内部 RAM
   同速 77.5 MB/s ✓），反倒是 **JPEG 解码器的输入/输出缓冲必须在 PSRAM**
   （2D-DMA 限制）。别用 LovyanGFX（§5）；若要换 MIPI-DSI 屏，那才是另一套驱动。

## 3. 与 ESP32-S3 版的关键差异（设计前重想）

| 维度 | ESP32-S3（旧） | ESP32-P4（本）对设计的影响 |
|---|---|---|
| USB | 全速 12 Mbps | **High-Speed 480 Mbps** ⇒ 带宽不再是天花板，"压缩率 vs 解压开销"的取舍要重算 |
| 解码 | 无硬件解码器，全靠 CPU | **有硬件 JPEG 编解码**（`SOC_JPEG_CODEC_SUPPORTED` / `_DECODE_` / `_ENCODE_` ✓）和 **H.264 编码器**（`SOC_H264_ENCODER_SUPPORTED` ✓）⇒ "CPU 越快软解越强"的前提变了，可能把 CPU 让给别的事更值 |
| 面板 | LCD_CAM i80 16-bit 并口 | **P4 也有 LCD_CAM，而且支持 i80/RGB**（`SOC_LCDCAM_I80_LCD_SUPPORTED` / `SOC_LCDCAM_RGB_LCD_SUPPORTED` ✓），另有 MIPI-DSI/CSI、PARLIO、ISP(DVP)；**所以要换的是库（LovyanGFX → IDF esp_lcd），不是总线**。同一块 i80 屏可以继续用 |
| 架构 | 双核 Xtensa LX7 | RISC-V（工具链 `riscv32-esp-elf`，IDF ≥ 5.3） |

## 4. 可以直接搬的

- 主机侧 PUD 协议（EP1 分帧、`PUD_CMD_*`、`GET_CAPS` 报 `frame_max`、`DECODER_TYPE`）
- `boot_request.c` / `scripts/flash-recover.sh`（一键进下载态）
- decoder 分层（`decoder.c` 调度 + 具体解码器，坐标/窗口参数化）
- 调试方法论：`RTC_NOINIT` 面包屑（挂住的线程既不出 `ESP_LOG` 也不出
  `esp_rom_printf`）、证据分级、四行前置门（已验证/仍未知/最小改动/生效判据）


## 5. 显示层：不依赖 LovyanGFX，自己写在 IDF esp_lcd 之上

**决定**：本移植不再引入 LovyanGFX。原因不是偏好，是 ESP32-S3 那一版实测出来的
四条硬伤：

1. `Bus_Parallel16.cpp:98` **丢弃 `esp_lcd_new_i80_bus()` 的返回值** ⇒ 初始化失败
   也毫无提示。S3 上实测：`LCD_CAM.lcd_clock.val == 0`（外设根本没配起来），
   而日志照样打 `init: display ready` ✗。
2. `_init_pin()` **无条件**把 16 根数据线全部 `gpio_matrix_out` ⇒ 在 octal PSRAM
   的板子上直接抢掉 GPIO33-37（这就是那次无限重启的根因，8 MB PSRAM 报废）✗。
3. 同步契约是**隐式**的：`writePixelsDMA()` 故意不 `endWrite()`，靠下一次
   `setAddrWindow` 等 `LCD_CAM_START` 清位来同步 ⇒ 一旦出问题表现为**无限自旋**
   （最后被中断看门狗打死），而不是返回错误 ✗。
4. 它是 vendor 进仓库的一份副本，修不了上游，只能绕 ✗。

P4 上更是非换不可：MIPI-DSI 在 LovyanGFX 里基本没有支持。

**做法**：显示层自己实现，建立在 IDF 官方 `esp_lcd` 之上
（`esp_lcd_new_panel_io_i80` / `esp_lcd_new_panel_rgb` / `esp_lcd_new_panel_dsi`
按面板接口选），我们只负责：引脚与总线配置、背光、旋转、以及"把一块像素刷上去"
这几件事。

### 必须保持的 API 契约（这样 decoder 层可以原样继承）

沿用 ESP32-S3 项目 `main/include/tft.h` 的那四个入口，签名不变：

```c
void tft_driver_init(void);
void tft_video_flush(int xs, int ys, int xe, int ye, void *vmem, uint32_t len);
void tft_async_video_flush(int xs, int ys, int xe, int ye, void *vmem, uint32_t len);
void tft_async_video_wait(void);
```

规则（写实现时必须照做，它们是对上一版隐式契约的**显式化**）：

- `tft_video_flush()` 返回时，像素**一定已经送完**；
- `tft_async_video_flush()` 只保证提交，`tft_async_video_wait()` 是**唯一**的显式
  同步点（调用后缓冲区才可复用）；
- 源缓冲必须 **DMA 可访问**；**P4 上 PSRAM 也行**（i80 驱动显式支持外部内存，
  实测 PSRAM 源 77.5 MB/s vs 内部 RAM 源 77.6 MB/s，同速 ✓）。S3 那条"DMA 源
  必须在内部 RAM"的结论**不要**照搬到 P4 ✗；
- **所有 `esp_lcd_*` 返回值都要判**，失败就 `ESP_LOGE` 并让初始化失败，
  不要"日志成功、硬件没配"；
- 每次刷新的窗口/方向换算写在显示层内部，decoder 只给面板坐标。

**开工第一步**（也是 §2 第 1 条）：把 PSRAM 与面板/摄像头的引脚占用列清楚，
再决定总线宽度与接口（i80 / RGB / MIPI-DSI）。S3 那次就是顺序反了。
