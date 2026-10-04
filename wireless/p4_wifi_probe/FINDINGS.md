# P4 + C6（ESP-Hosted）链路探针：实测记录

目标：在接显示之前，先证明"主机经 WiFi 推数据到 P4"这条链能跑、能跑多快。
判据（先写死）：hosted 报 C6 的 INIT event → 连上 AP 拿到 IP →
TCP sink 持续吞吐 **≥1 MB/s**（480x320 JPEG q60 @60fps 只需 0.66 MB/s）。

## 结论：链路已打通 ✓✓（远超需求）

| 测量 | 结果 |
|---|---|
| TCP sink 10 s | **5.46 MB/s（43.7 Mbps）** ✓ |
| TCP sink 60 s 长跑 | **326.6 MB / 60.01 s = 5.44 MB/s（43.5 Mbps）**，全程无中断 |
| ping（同期） | 8/8，RTT min/avg/max = **2.16 / 4.46 / 11.31 ms**，mdev 2.8 ms |
| SDIO 错误 / 复位 | **0 / 0** |
| 相对需求 | q60 JPEG @60fps 需 0.66 MB/s ⇒ **8 倍余量**；q90 @60fps 需 1.65 MB/s ⇒ 3.3 倍 |

## 真正的根因：**host 组件版本**（不是硬件、不是引脚、不是信号）

```
host = esp_hosted 2.12.0（IDF examples/wifi/iperf 给 P4 钉的 "~2"）
  → E H_SDIO_DRV: sdio_write_task: Failed to send data: 258 (0x102 ESP_ERR_INVALID_ARG)
  → E H_SDIO_DRV: Unrecoverable host sdio state  → 重启宿主/挂住，6~16 秒一次 ✗
host = esp_hosted 3.0.9（host_performs_slave_ota 例子的 '*'）
  → 一切正常，43.5 Mbps 稳跑 60 s ✓
```

**单变量逻辑**（说明为什么能确定是 host 而不是 CP）：

1. 先只改从机：用主机侧 OTA（partition 法）把 C6 从"版本报 0.0.0 的老固件"
   升到 **2.12.13** ⇒ host 2.12.0 下手写路径**仍然失败** ✗
2. 再只改主机：2.12.0 → **3.0.9** ⇒ **通了** ✓

## 已排除的变量（各自都是单变量实验）

| 变量 | 结果 |
|---|---|
| SDIO 时钟 40 → 20 MHz | 症状一样（甚至更早失败）⇒ 不是总线速率/信号 ✗ |
| 关掉 `ESP_HOSTED_MEMPOOL_PREFER_SPIRAM` | 立刻回到启动 OOM（`init function ... failed (0x101)`）⇒ 该项**必需** ✗ |
| 从机固件太老 | OTA 升级到 2.12.13 后，配 2.12.0 主机仍失败 ⇒ 不是从机的锅 ✗ |
| AP 侧（频段/认证） | 扫描 ch9 WPA2_PSK、关联、DHCP、ping 全通 ✓ |
| 引脚/极性 | 日志逐脚印证原理图推导：`CLK18 CMD19 D0-14 D1-15 D2-16 D3-17 RESET54` ✓ |

**我自己的错误（值得记）**：第一次关联失败（reason 2 `AUTH_EXPIRE` / 205 `CONNECTION_FAIL`）
是我转录密码时丢了结尾的 `.`。路由器真实 PSK 从本机 NetworkManager 读出才确认
（`nmcli -s connection show <SSID>`）⇒ **改凭据类配置前，先用可指认来源核对**。

## 复现配方（P4 + 这块板）

```yaml
# main/idf_component.yml —— 别照抄 IDF iperf 例子的 "~2"，那个组合在本板是坏的
espressif/esp_hosted:      "~3"        # 实测 3.0.9 可用
espressif/esp_wifi_remote: ">=0.10,<2.0"   # 解析到 1.1.6
```

必需 sdkconfig（否则起不来/不稳）：`SPIRAM_XIP_FROM_PSRAM`、`CACHE_L2_CACHE_256KB`、
`CACHE_L2_CACHE_LINE_128B`、`FREERTOS_HZ=1000`、`SLAVE_IDF_TARGET_ESP32C6`、
P4 rev v1.0 两条（`SELECTS_REV_LESS_V3` / `REV_MIN_100`）、16 MB flash。
3.x 里 `ESP_HOSTED_MEMPOOL_PREFER_SPIRAM` / `SDIO_CLOCK_FREQ_KHZ` / streaming 等
符号**已被重构掉**（2.x 才需要那些）。

WiFi 连接自己写（netif → 默认事件循环 → wifi 三步，每步判返回值）：
IDF 的 `protocol_examples_common` 在本板会先去做以太网初始化（EMAC 无 PHY ⇒ abort），
且它在 remote WiFi 路径下没建默认事件循环 ⇒ `esp_event_handler_register` 返回
`ESP_ERR_INVALID_STATE(0x103)` 后 abort。

## 从机固件怎么刷（两条路，都已实测可用）

1. **主机侧 OTA（推荐，零接线）**：`host_performs_slave_ota` 例子 + **partition 法**
   —— 把从机镜像写进主机自己的 `slave_fw` 分区（`esptool write-flash --force 0x5F0000 <img>`），
   主机启动后经 SDIO 推给 C6。实测：**1 282 464 B / 9 s 传完并生效** ✓。
   从机镜像由组件自带源码编译：`idf.py -C managed_components/espressif__esp_hosted/slave -B <dir> set-target esp32c6 && build`。
2. **H4 排针 + USB-TTL**（恢复/首次刷写）：**1=IO9 短到 GND**、**2=GND**、
   **3=C6_U0RXD**、**4=C6_U0TXD**；TTL TX→3、RX→4；**按住 BOOT 上电**。
   （微雪 FAQ 原文与原理图推导一致。）

## 仍未做

- 主机 3.0.9 会报 `major version mismatch — OTA coprocessor from host`
  （从机还是 2.12.x），它会自动走"compatible streaming mode"。可以再 OTA 一次把从机
  也升到 3.x 消除该警告（可选，当前吞吐已够）。
- 真正要做的下一步：**TCP 收 JPEG → P4 硬件解码 → i80 面板**（复用
  `pico_dm_qd3503728_esp32p4_idf` 里已验收的显示层与 JPEG 基准代码）。

## C6 从机升级到 3.0.9 之后的复测

从机固件升级到 **3.0.9**（与 host 同版本），日志确认匹配且聚合生效：

```text
I eh_init_evt: esp-hosted fw versions: host=3.0.9 coprocessor=3.0.9 (match)
I eh_init_evt: SDIO SW_AGGR negotiated (e2h=15872B h2e=15872B)   ← 旧从机只能走兼容模式
```

| 测量 | 结果 |
|---|---|
| 链路 TCP sink（3 次 × 15 s） | **4.95 / 5.01 / 5.03 MB/s**（39.6~40.2 Mbps），离散 1.6% |
| 端到端投屏 60 fps 节流 20 s | 59.64 fps、**0 bad**（1193 帧）|
| 端到端预编码猛推 10 s | **114.5 fps**、0 bad（设备侧 113~115 fps，decode 1.76 ms、flush 3.96 ms）|

**不可比的差异（如实记录）**：升级前"3.0.9 host + 2.12.13 从机（兼容模式）"单次 60 s 测得
5.44 MB/s，比升级后的 4.95~5.03 高约 8%。该差异**大于**同固件的重复性离散（1.6%），
但**没有做同时刻 A/B**（两次测量相隔数十分钟，2.4G 环境可能已变）⇒ 不据此判断哪版更快。
两组都远超需求（q60 JPEG @60fps 需 0.66 MB/s）。

端到端上限升级前后**没有变化**（114.8 → 114.5 fps）⇒ 瓶颈仍是 TCP 往返/窗口，
与从机固件无关。

## UDP 传输 + 丢帧策略的实测（投屏 demo 侧）

设备侧统计（`shown` / `dropped` / `bad`）为判据，PC 侧只作参考：

| 目标 fps | 设备 shown fps | 稳态丢弃 | 备注 |
|---|---|---|---|
| 60 | 59.5~59.7 | 仅启动爬坡 18 帧 | 工作点 |
| 100 | 97~99 | ≈1 帧/秒 | — |
| 130 | 120~128 | ≈1~2 帧/秒 | — |
| 150 | **145.6~147.5** | ≈2~4% | UDP 的实用上限 |
| 175 | 130（掉下来）| 29 → 195（猛增）| 正好撞上设备流水线上限 |
| 200 | 30 | 279 | — |

- 崩溃点 **175 fps ≈ 设备流水线上限**（decode 1.69 ms + flush 3.96 ms = 5.67 ms/帧 ⇒ 176 fps），
  说明瓶颈已从传输转到设备本身。
- 20 秒 120 fps 稳定性：**118.5 fps、≈1 帧/秒丢弃、0 bad**。
- **0 bad** 是丢帧策略的直接证据：未组装完的帧（有分片丢失）**从不**交给解码器，
  所以屏幕上不会出现残帧；代价是丢弃率随速率上升。
- **测量方法教训**：UDP 不限速时 PC 侧计数无意义（`send()` 不阻塞，包在本地就被丢），
  判据必须取设备侧；脚本已加 `--udp` 必须限速的保护。
