# HANDOFF —— 交接须知（ESP32-P4 移植）

一句话：**显示与解码这条链已经在 P4 上打通并量化完毕；还差 USB 主机链路和 PUD
协议层。** 下面按"接手第一天要做什么"的顺序写。

---

## 1. 现在能跑什么

`idf.py build flash` 之后，串口会依次看到：

1. 芯片/堆/复位原因；
2. 面板初始化（引脚、复位脉冲、`pclk requested -> actual`、`swap_color`）；
3. **总线自检**：整帧一笔的 MB/s、纯 CPU 填充的 ms/帧；
4. **硬件 JPEG 基准**：元素序自检、四张内置图的解码时间、零拷贝上屏、串行 vs 乒乓；
5. 之后**三个相位各 8 秒无限轮转**，且都在连续重画（WR 一直有波形，示波器随时可量）：
   - **A 方向测试图**（判据见下）
   - **B 棋盘**（看信号质量）
   - **C 硬件解码出来的 `xfce` 桌面截图**（整条解码链路）

当前实测（40 MHz 总线）：总线 77.6 MB/s、JPEG 解码 1.76 ms/帧、
解码+上屏串行 5.72 ms/帧（174.7 fps）⇒ **对 60 fps 有 3 倍余量** ✓

## 2. 硬件事实（别猜，都审计过了）

- 板子：微雪 **ESP32-P4-WIFI6**，P4 rev **v1.0**，16 MB flash，**32 MB PSRAM**。
- 面板：QD3503728 扩展板（ILI9488，**8080 16-bit 并口**），**可以直接插，不用飞线**；
  25 个信号里 23 个落在空闲 GPIO，另外两个落在 GPIO24/25（排针的 `usbd1_n/p`，
  默认归 USB 功能，初始化时已切回普通 GPIO）。
- 引脚定案（含翻译链与冲突审计）：**`notes/pin-map.md`**（旋转 1 / 横屏 480x320）。
- 烧写：板上 USB 转串口的 DTR/RTS 接了 EN/BOOT ⇒ `idf.py -p <PORT> flash` 即可，
  **不用按 BOOT** ✓（`boot_request.c` 那套"应用自己进下载态"在这里用不上，
  见 §6）。
- P4 原生 USB **不在 Type-C 上**：Type-C 只是 UART 桥，USB 在 **P1（MX1.25 4pin）**。

## 3. 怎么构建 / 烧写 / 看日志

```bash
. ~/esp/esp-idf-v6.1/export.sh   # P4 要 IDF ≥ 5.3；本机 v6.1（riscv32-esp-elf）
idf.py build
idf.py -p <PORT> flash
idf.py -p <PORT> monitor         # 退出：Ctrl+]
```

`sdkconfig.defaults` 里有**两个必设项**（少了就编不过/烧不进，见 §5 的坑 #1）：
`CONFIG_ESP32P4_SELECTS_REV_LESS_V3=y`、`CONFIG_ESP32P4_REV_MIN_100=y`。

看一眼就知道对不对的**自检判据**（相位 A，自顶向下 5 条横带）：

- (0,0) 处有 **16x16 白块**（在左上角）；
- 红 / 绿 / 蓝**自上而下**（各带 32px 黑格）；
- 第 4 带是 **1px 竖条纹**、第 5 带是 **1px 横条纹**。

白块跑到别的角 ⇒ MADCTL 镜像位错；第 4/5 带条纹方向互换 ⇒ MV 那支错；
出现随机色块/"油画感" ⇒ 信号质量（先降时钟再查线）。

## 4. 代码结构

| 文件 | 职责 |
|---|---|
| `main/panel.c` | 显示层：i80 总线、引脚/驱动能力、ILI9488 初始化、**旋转与窗口换算**、背光、`tft_*` 契约实现 |
| `main/main.c` | 自检与基准的调度：总线自检 → JPEG 基准 → 三相位轮转 |
| `main/jpeg_bench.c` | 硬件 JPEG：引擎/缓冲、元素序自检、解码计时、零拷贝上屏、串行 vs 乒乓 |
| `main/include/tft.h` | **对外契约**（与 ESP32-S3 项目同名同签名，decoder 层要能原样继承） |
| `main/include/board_pins.h` | 引脚、原生/逻辑分辨率、旋转、总线时钟 |
| `main/assets/jpeg_assets.h` | 内置测试图（**自动生成**，`scripts/make-jpeg-assets.py`） |
| `scripts/make-jpeg-assets.py` | 生成上面那份头文件（色条自检图 + xfce 三档质量） |
| `main/boot_request.c` | **当前不参与构建**（P4 没有 `RTC_CNTL_FORCE_DOWNLOAD_BOOT`，本板也不需要）|

## 5. 已经踩过、别再踩的坑

细节与判据都在 `notes/`，**这里不重复**：

| 坑 | 一句话 |
|---|---|
| 芯片 rev | rev v1.0 必须设 `ESP32P4_SELECTS_REV_LESS_V3` + `REV_MIN_100`，否则拒烧 → [bring-up.md](notes/bring-up.md) |
| 面板不亮/花屏 | 缺 RESX 脉冲（整屏白）；`lcd_cmd_bits`/`lcd_param_bits` 必须 **8**；`swap_color_bytes` 必须 **0**；19 根线驱动能力拉平到 `CAP_1` → [panel-ili9488-i80.md](notes/panel-ili9488-i80.md) |
| 写时钟 | 请求值被取整到 **80/n MHz**（请求 50 ⇒ 实际 80，显示不对 ✗）⇒ 定档 40 MHz → [panel-ili9488-i80.md](notes/panel-ili9488-i80.md) |
| 刷屏慢 | 每笔 ≈90~120 µs 固定开销 ⇒ **整帧一笔**；CPU 填充与总线同量级 ⇒ **双缓冲** → [lcd-transfer-throughput.md](notes/lcd-transfer-throughput.md) |
| 缓冲放哪 | i80 源**可以是 PSRAM** ✓；**JPEG 缓冲必须 PSRAM** → [general/esp32p4-dma-buffer-placement.md](notes/general/esp32p4-dma-buffer-placement.md) |
| "整屏一笔复位" | 假象：`max_transfer_bytes` 配小了 + `ESP_ERROR_CHECK` ⇒ abort（现改为只 `ESP_LOGE`） |
| USB 复位 | **不要**用 `esptool --before usb_reset`（S3 实测打死主机 xHCI）→ S3 项目 `AGENTS.md` §4 |

## 6. 下一步（按建议顺序）

1. **USB 主机链路**（等 P1 的 MX1.25 4pin 线；用户自备）。P4 原生 USB 是
   **HS 480 Mbps**，主机侧 PUD 不用改；设备侧要用 USB Device 栈（`usb_device` +
   CDC/自定义类）。注意 P4 的 USB 引脚默认归 USB 功能，我们暂时没接管它们。
2. **PUD 协议层**：从 S3 项目搬 `main/pud.c` 那套（`PUD_CMD_*`、`GET_CAPS` 报
   `frame_max` / `DECODER_TYPE`、EP1 分帧）。关键映射：
   - `DECODER_TYPE` 用 **JPEG**（0/1 的槽位就是 JPEG）：设备侧换成
     `jpeg_bench.c` 里那套硬件解码即可，**主机侧不用改** ✓；
   - 分辨率/旋转：用 `board_pins.h` 的 `TFT_LOG_HOR_RES/VER_RES`（= 480x320，
     与 S3 的 `pud.c` 算法一致）；
   - 刷法：**整帧一笔**（每笔死时间 ≈90~120 µs，小笔白扔带宽）；
   - 缓冲：解码输出在 **PSRAM**（硬性要求），i80 直接读它 ⇒ 双缓冲用
     `tft_async_video_flush()` / `tft_async_video_wait()` 乒乓。
3. **触模**（FT6236，I2C SDA/SCL = GPIO23/22，板子上已就位）：直接复用 S3 项目的
   `board_touch.c`。
4. **未解释的项**（有空再做，不影响 60 fps）：乒乓只比串行快 5%，分解数据显示
   解码在负载下速度不变（不是 CPU 争用），但"等总线"时间也没减少 ⇒ 怀疑
   **PSRAM 上 JPEG 2D-DMA 写 + i80 GDMA 读并行互相拖累**。含义：若墙在内存带宽，
   上第二个核也救不了。见 `notes/jpeg-hardware-decode.md`。

## 6.5 无线这条路（已打通，可选继续）

片内 C6 + ESP-Hosted **已经端到端验证**：链路 **5.44 MB/s / 43.5 Mbps**（60 s 长跑，
ping RTT 4.5 ms），端到端投屏 **60 fps、0 丢帧**（TCP JPEG → 硬件解码 → i80 双缓冲）。
演示工程在工作区 `../p4_wireless_display/`，链路探针与完整证据链在
`../p4_wifi_probe/`（`FINDINGS.md`）。

**唯一硬约束**：host 组件用 `espressif/esp_hosted "~3"` —— IDF 官方例子给 P4 钉的
`"~2"` 在本板上数据面直接崩（`0x102` → `Unrecoverable host sdio state`）。
细节见 `notes/wifi-over-c6-hosted.md`。

想继续做的话：桌面采集实时投屏（取代现在的合成动画）、UDP + 丢帧策略换更低延迟、
或把 C6 从机也升到 3.x 消掉版本警告。

## 7. 提交前要处理的事

`main/main.c` 的相位轮转与 `main/jpeg_bench.c` 目前**既是自检也是基准**（属于
"探针"性质的代码）。按仓库约定（`AGENTS.md` §1）**提交前要清干净**，二选一：

- 收成 Kconfig 开关（默认关，需要时打开）；或
- 只保留相位 A/B（面板自检），把基准部分删掉，需要时再从笔记里复制回来。

仓库**还没有 git 仓库**（首次提交要 `git init`），并且**未经明确指示不要 commit**。
