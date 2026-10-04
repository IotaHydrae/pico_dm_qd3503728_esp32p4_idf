# wireless/ —— P4 无线投屏的 PC 侧与 C6 工具链

> 本目录是**无线这条路**的三个工程：链路探针、端到端投屏 demo、主机侧给片内 C6 刷固件的
> OTA 工具。设备端固件（面板 / JPEG 硬解 / i80 上屏）在仓库根的 `main/`，
> **结论与踩坑一律写在 `../notes/`**，这里只放能跑的工程。

| 子工程 | 是什么 | 入口 |
|---|---|---|
| `p4_wifi_probe/` | 链路吞吐探针：hosted 配置配方 + 单变量实验的完整证据链 | `FINDINGS.md`、`README.md` |
| `p4_wireless_display/` | 无线显示器 demo：PC 推 JPEG → P4 硬件解码 → i80 上屏（实测 60 fps、0 丢帧） | `README.md` |
| `p4_wifi_ota/` | 主机侧给 C6 刷固件（**含留档的两套 C6 镜像**，可随时回退） | `c6_firmware/README.md` |

三个子工程最初只是磁盘上的开发脚手架，现并入本仓。将来若要抽成独立仓
（`pud-wireless` / `esp32p4-wireless-display`），**整目录摘出去即可** —— 所以内部互相引用
一律用相对路径（如 `../p4_wifi_probe/FINDINGS.md`），摘走后不会断。

## 三条最贵的结论（细节见 `../notes/`）

1. **host 组件必须是 `espressif/esp_hosted "~3"`** —— IDF 官方 P4 例子钉的 `~2` 在本板
   数据面直接崩（`0x102` → `Unrecoverable host sdio state`）。
   见 `../notes/general/esp-hosted-version-pinning.md`。
2. **PC 侧采集必须选对门户授权框那一项**（`Share "<monitor>"`，第一条）—— 选 virtual screen
   会拿到一块新建的空屏：投屏"成功"、设备侧照样 `0 bad`，但画面永远不是你的桌面。
   见 `../notes/wayland-portal-capture.md`。
3. **`CONFIG_LWIP_UDP_RECVMBOX_SIZE=64`**（默认 6）—— 采集源成串出帧，突发会打爆默认
   邮箱 ⇒ 丢分片 ⇒ 整帧被丢帧策略丢掉。见 `../notes/wifi-over-c6-hosted.md`。

## 约定

- 凭据（WiFi PSK 等）**只允许**出现在各自的 `sdkconfig`；`sdkconfig.defaults` 进仓、
  `sdkconfig` 不进仓（三个子工程的 `.gitignore` 都已覆盖）。
- `build/`、`managed_components/`、`__pycache__/`、`dependencies.lock` 不进仓。
- 三个子工程的 CMake 工程名与目录名保持一致，便于单独 `idf.py build`。
