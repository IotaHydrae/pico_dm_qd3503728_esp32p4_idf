# 这块板上的 WiFi：片内 C6 + ESP-Hosted

> P4 没有 WiFi 射频，板上那颗 **ESP32-C6 就是它的网卡**：P4 侧照常调用标准
> `esp_wifi_*` API，由 `esp_wifi_remote` 经 RPC 转发给 C6，数据面走 **SDIO**。
> **前提是 host 组件用 3.x** —— IDF 官方例子给 P4 钉的 `esp_hosted "~2"` 在本板上
> SDIO 写路径直接崩（见 [general/esp-hosted-version-pinning.md](general/esp-hosted-version-pinning.md)）。

## TL;DR

- 依赖：`espressif/esp_hosted: "~3"`（实测 3.0.9 可用）+ `esp_wifi_remote`；
  slave target 在 P4 上默认就是 `esp32c6`（`CONFIG_SLAVE_IDF_TARGET_ESP32C6=y`）。
- 引脚（日志逐脚印证原理图推导，**与面板无冲突** ✓）：
  `CLK=18 CMD=19 D0=14 D1=15 D2=16 D3=17 RESET=54`（C6 侧是固定脚 IO19/18/20-23），
  另有一根 `C6_IO2 → P4 GPIO6` 的握手线。
- **C6 出厂固件可用、通常不需要刷**（微雪 FAQ 也这么说）；真要升级有两条路，
  见下面"刷从机固件"。**本板现况：C6 已升到 3.0.9，与 host 同版本**
  （日志 `fw versions: host=3.0.9 coprocessor=3.0.9 (match)`，且 **SDIO SW_AGGR 协商成功**）。
- 实测吞吐：host 3.0.9 + 从机 2.12.13（兼容模式）单次 60 s 得 **5.44 MB/s（43.5 Mbps）**；
  从机也升到 3.0.9 后连测 3 次得 **4.95 / 5.01 / 5.03 MB/s（39.6~40.2 Mbps）**。
  两者差约 8%，**大于同固件的重复性离散（1.6%），但没做同时刻 A/B ⇒ 不下"新版更慢"的结论**
  （也可能是 2.4G 环境变化）。无论哪组，对 480x320 JPEG q60 @60fps（需 0.66 MB/s）
  都有 **7~8 倍余量**。ping RTT 平均 4.5 ms。
- 必需 sdkconfig（缺了起不来或直接崩）：`SPIRAM_XIP_FROM_PSRAM`、`CACHE_L2_CACHE_256KB`、
  `CACHE_L2_CACHE_LINE_128B`、`FREERTOS_HZ=1000`（hosted 要求 1000，100 会抖）。
- **UDP 收大流量时还要 `CONFIG_LWIP_UDP_RECVMBOX_SIZE=64`**（默认 6）：接收端是"成串出帧"
  的采集源时，突发会把默认邮箱打爆 ⇒ 丢分片 ⇒ 整帧被丢帧策略丢掉。实测同一个发送器
  在邮箱=6 时设备只显示 **9.4/30 fps**、=64 时 **30.0/30** ✓（官方 P4+hosted 配置用的也是 64）。

## 现象与结论：为什么"照抄官方例子"会掉进坑里

| host 组件 | 结果 |
|---|---|
| `esp_hosted 2.12.0`（IDF `examples/wifi/iperf` 给 P4 钉的 `~2`） | 控制面通（扫描/关联/DHCP/ping），**数据面一写就崩**：`sdio_write_task: Failed to send data: 258`（0x102 `ESP_ERR_INVALID_ARG`）→ `Unrecoverable host sdio state` → 重启宿主/挂住，6~16 秒一次 ✗ |
| `esp_hosted 3.0.9`（`host_performs_slave_ota` 例子的 `'*'`） | 一切正常，43.5 Mbps 稳跑 60 s ✓ |

单变量逻辑：先把 C6 从机 OTA 升到 2.12.13 ⇒ 配 2.12.0 host **仍然失败** ✗；
再只把 host 换成 3.0.9 ⇒ **通** ✓（所以决定因素是 host 版本）。
之后再把从机也升到 **3.0.9**（与 host 同版本）⇒ 版本警告消失、SW 聚合生效 ✓。

## 必需的整块配置（来源都写清楚，别当魔法）

- 微雪 `10_wifistation/sdkconfig.defaults`（**这块板**的实测配置）：
  `SPIRAM_XIP_FROM_PSRAM`、`CACHE_L2_CACHE_256KB`、`CACHE_L2_CACHE_LINE_128B`、
  `FREERTOS_HZ=1000`、`ESP_MAIN_TASK_STACK_SIZE=10240`。
  （微雪那份还写 `ESP_WIFI_SOFTAP_SUPPORT=n`，但在本工程里**没有生效**：
  实测生成物是 `=y`，漂移检查抓到的；功能上未观察到影响，故不再声称它可关。）
- IDF `examples/wifi/iperf/sdkconfig.defaults`（官方为 P4+hosted 测过）：
  `LWIP_TCPIP_TASK_PRIO=23`、`LWIP_IRAM_OPTIMIZATION`、`SYSTEM_EVENT_TASK_STACK_SIZE=4096`、
  `PARTITION_TABLE_SINGLE_APP_LARGE`、PHY 两项。官方那份还**关掉了 INT/TASK 看门狗**
  （为了极限吞吐），本工作区**故意保留**看门狗 —— 卡住时它是唯一能救回设备的机制。
- 2.x 时代那批内存开关（`ESP_HOSTED_MEMPOOL_PREFER_SPIRAM`、
  `ESP_HOSTED_DFLT_TASK_FROM_SPIRAM`、`SDIO_CLOCK_FREQ_KHZ`、streaming 模式选择）
  在 **3.x 里已被重构掉**；2.x 上它们是必需的（不设则启动 OOM：
  `init function ... has failed (0x101)`），3.x 上不需要也不存在。

## WiFi 连接自己写，别用 `protocol_examples_common`

`example_connect()` 在本板上会**先去做以太网初始化**（P4 的 EMAC 没有 PHY）
⇒ `esp_emac: reset timeout` → `abort()` ✗；它在 remote WiFi 路径下也没建默认事件循环
⇒ `esp_event_handler_register` 返回 `ESP_ERR_INVALID_STATE(0x103)` 后 abort ✗。
自己写三步（`esp_netif_init` → `esp_event_loop_create_default` → `esp_wifi_*`），
每步判返回值并打错误码，出问题一眼能看出是哪一步。

## 刷从机（C6）固件：两条路都实测过

1. **主机侧 OTA（零接线，推荐）**：用组件自带的 `examples/host_performs_slave_ota`
   的 **partition 法** —— 把从机镜像写进主机自己的 `slave_fw` 分区，主机启动后经 SDIO 推给 C6。
   实测：**1 282 464 B / 约 9 s 传完并生效** ✓（从机版本随之从"报 0.0.0 的老固件"变为 2.12.13）。
   注意：写镜像要 `esptool write-flash --force 0x5F0000 <img>`（镜像是 C6 的，
   esptool 会拒绝把它当 P4 镜像；这里它只是**数据分区的内容**）。
   **3.x 的从机工程路径变了**：2.x 在组件的 `slave/`，3.x 在
   `examples/ota/coprocessor_ota/cp/`（自带 4 MB A/B OTA 分区表，从机侧有回滚）。
   `idf.py -C <该工程> -B <builddir> set-target esp32c6 && idf.py -C … build`。
   **离线构建的坑**：该工程的 `main/idf_component.yml` 默认从组件仓库拉 `esp_hosted`，
   访问不到 `components-file.espressif.com` 时配置阶段直接失败 ✗；改成指向本地组件时
   `override_path` **必须写绝对路径**（相对路径的解析基准与文档所述不一致，实测失败）。
2. **H4 排针 + USB-TTL（恢复/首次刷写）**：H4 = **1:IO9、2:GND、3:C6_U0RXD、4:C6_U0TXD**
   （原理图坐标重建所得，与微雪 FAQ 一致）；TTL 的 TX→3、RX→4、GND→2；
   **先给 IO9 短到 GND**，**按住板子 BOOT 再上电**（BOOT 让 P4 别去控 C6 复位），然后
   `esptool --chip esp32c6 ... write-flash 0x0 bootloader.bin 0x8000 partition-table.bin 0xd000 ota_data_initial.bin 0x10000 network_adapter.bin`。

## 端到端（投屏 demo）：传输方式决定上限

TCP 收 JPEG → 硬件解码 → i80 上屏（双缓冲乒乓）。两种传输都实测过：

| 传输 | 端到端上限 | 丢帧 | 瓶颈 |
|---|---|---|---|
| **UDP + 丢帧（推荐）** | **~150 fps**（150 目标实测 145~147）| ~2~4%（速率越高越多）| 接近设备上限 |
| TCP 流（长度前缀）| 114.5 fps | 0 | **TCP 往返/窗口** |
| 设备内部（decode+flush）| ~176 fps | — | decode 1.69 ms + flush 3.96 ms |

- **UDP 的丢帧策略**：分片带头（帧号/片号/片数/总长），**收到更新的帧号就丢弃当前
  未组装完的帧** ⇒ 永远显示"最新的完整帧"，不排队追旧数据 ⇒ 延迟有界、全过程
  **0 bad**（残帧从不喂给解码器）。
- 崩溃点（175 fps）正好等于设备流水线上限 ⇒ 再往上加速率没有意义。
- **测量教训**：UDP 的 `send()` 不阻塞，不限速时 PC 侧 fps 是"往内核塞包的速度"、
  毫无意义 ✗ ⇒ UDP 测试必须限速，判据取**设备侧的 shown/dropped**。

## 边界与陷阱

- 主机与从机**同版本**时最干净（本板现况 3.0.9+3.0.9）：无版本警告、`SDIO SW_AGGR`
  协商成功。旧从机（2.12.13）配 3.x 主机也能跑，但只能走 `compatible streaming mode`。
- **主机与从机的 SDIO streaming/packet 模式必须一致**：一边 streaming 一边 packet 会直接
  `SDIO mode mismatch ... Aborting` ✗（换组件大版本后尤其要核对）。
- 凭据（SSID/密码）只放**被 gitignore 的 `sdkconfig`**；核对凭据用可指认来源
  （Linux 上 `nmcli -s connection show <SSID>` 能读出真实 PSK），别靠记忆转录。
- 开监测口（`idf.py monitor`）会因 DTR/RTS **复位板子**；只想看日志用 `--no-reset`。

## 相关

- [general/esp-hosted-version-pinning.md](general/esp-hosted-version-pinning.md) —— 版本组合为什么是硬约束
- 演示工程（不在本仓）：工作区 `../p4_wireless_display/`（端到端投屏）、
  `../p4_wifi_probe/`（链路吞吐探针与完整证据 `FINDINGS.md`）
