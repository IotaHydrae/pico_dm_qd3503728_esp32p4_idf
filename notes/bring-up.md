# ESP32-P4 首启：芯片 rev、工具链与板级事实

> P4 需要 ESP-IDF ≥ 5.3；**rev v1.0** 的芯片必须显式选中"rev < v3"的 sdkconfig，
> 否则 esptool 直接拒烧（`requires chip revision in range [v3.0 - v3.99]`）。

## TL;DR

- 工具链：IDF **v6.1** + `riscv32-esp-elf`（P4 是 RISC-V）；5.2 编不了 P4。
- 两条 sdkconfig 必设：`CONFIG_ESP32P4_SELECTS_REV_LESS_V3=y`、
  `CONFIG_ESP32P4_REV_MIN_100=y`（少了就烧不进去，见下）。
- 烧写**不需要按 BOOT**：板上 USB 转串口的 DTR/RTS 接了 EN/BOOT，`idf.py flash` 即可。
- 微雪 `ESP32-P4-Platform` 参考工程的显示走 **MIPI-DSI + ST7701/JD9365 BSP**，
  **不能**照抄到 i80 面板；只有 `00_board_check` 这类板级自检有用。

## 环境（版本敏感，换版本要重测）

| 项 | 值 |
|---|---|
| 主机 IDF | v6.1（另一套 5.2 留给 ESP32-S3 项目，别混用） |
| 工具链 | `riscv32-esp-elf` |
| 芯片 | ESP32-P4，Dual Core + LP Core，**revision v1.0**，360 MHz（最高 400） |
| Flash / PSRAM | 16 MB flash；**32 MB PSRAM**（封装内，**与面板引脚无冲突**） |
| 串口 | 板上 USB 转串口（CH34x 一类）⇒ `/dev/ttyACM0` |

## 现象与修法：烧写被拒（rev 不匹配）

```text
E 'bootloader/bootloader.bin' requires chip revision in range [v3.0 - v3.99]
  (this chip is revision v1.0). Use the force argument to flash anyway.
```

修法（写进 `sdkconfig.defaults`，两条都要）：

```ini
CONFIG_ESP32P4_SELECTS_REV_LESS_V3=y
CONFIG_ESP32P4_REV_MIN_100=y
```

**为什么**：IDF v6.1 的 P4 bootloader 默认按 rev v3.0+ 构建，而本板是 v1.0；
选"rev less v3"后 bootloader 走 v1.0 兼容路径。（这两条也是参考仓库
`config/esp32p4_rev1_3.defaults` 的内容 ✓）

## 首启判据

```bash
source ~/esp/esp-idf-v6.1/export.sh
idf.py build
idf.py -p <PORT> flash monitor     # 出现 "Hard resetting via RTS pin..." ⇒ 不用按 BOOT
```

```text
I cpu_start: cpu freq: 360000000 Hz
I esp_psram: Adding pool of 32768K of PSRAM memory to heap allocator
I pud: PUD ESP32-P4 bring-up: 2 core(s), chip rev v1.0, reset reason 1
I pud: heap: internal ~586 KB free, PSRAM ~33.5 MB free
```

## 边界与陷阱

- P4 原生 USB **不在 Type-C 上**（Type-C 只是 UART 桥）；USB 在 **P1（MX1.25 4pin）**。
  接管 USB 时注意 GPIO24/25 默认归 USB 功能（见 [pin-map.md](pin-map.md)）。
- S3 那套"让应用自己进下载态"在本板用不上：本板 DTR/RTS 接好了；而且
  `main/boot_request.c` 依赖的 `RTC_CNTL_FORCE_DOWNLOAD_BOOT` 在 P4（RISC-V）上
  **不存在** ⇒ 该文件当前不参与构建（保留待哪天接管原生 USB 时按 P4 方式实现）。
- 参考工程 `00_board_check` 在不插 DSI 屏时会卡在显示初始化（任务看门狗点名 `main`）
  —— 这是"预期行为"，不是板子坏。
- 任何情况下**不要**用 `esptool --before usb_reset`（S3 上实测把主机 xHCI 控制器打死）。

## 相关

- [panel-ili9488-i80.md](panel-ili9488-i80.md) —— 面板点亮/转向/时钟定档
- [pin-map.md](pin-map.md) —— 引脚定案
- S3 项目同类记录（PSRAM 抢引脚、烧写恢复）：`../pico_dm_qd3503728_esp32s3_idf/AGENTS.md` §2/§4
