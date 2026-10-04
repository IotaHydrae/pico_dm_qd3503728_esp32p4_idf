# ESP-Hosted：host 与 co-processor 的版本组合是硬约束

> 用 ESP-Hosted 给没有 WiFi 的芯片（P4/H2 等）配网卡时，**host 组件版本和从机固件版本
> 是一个必须成对验证的组合**；照抄某个例子里钉的版本区间，可能得到一个
> "控制面全通、数据面一写就崩"的组合。

## TL;DR

- 症状（ESP32-P4 + 片内 C6 实测）：扫描、关联、DHCP、ping 全部正常，
  但**第一次数据收发就失败**：
  `sdio_write_task: Failed to send data: 258`（0x102 `ESP_ERR_INVALID_ARG`）
  → `Unrecoverable host sdio state` → 宿主重启或整机挂住。
- 换 host 组件版本（2.12.0 → 3.0.9）后同一块板、同一从机稳定 43.5 Mbps ✓。
  **不要**只按"registry 里的最新版本"或"某个例子钉的区间"推断兼容性。
- 判断依据是运行时日志里 host 与 coprocessor 各自报的版本
  （例：`esp-hosted fw versions: host=3.0.9 coprocessor=2.12.13`），
  以及**官方自己的警告**（`Version mismatch: Host [x] > Co-proc [y] ==>
  Upgrade co-proc to avoid RPC timeouts`）。

## 排错顺序（省时间）

1. **先看 transport 是否枚举成功**：`Received INIT event` / `Identified slave [...]` /
   `capabilities` 有没有出现。没出现 ⇒ 接线或传输配置问题，**别去调功能**。
2. **再看两条路径是否一致**：host 与 slave 的 SDIO 模式（streaming / packet）
   必须匹配，否则直接 `SDIO mode mismatch ... Aborting`。
3. **然后才看数据面**：控制面能过而数据面报 0x102/0x105 之类，
   优先怀疑**版本组合**，而不是信号质量。
4. 需要换版本时，**先动 host**（改一行依赖、重建几分钟），再考虑刷从机
   （从机升级可以用 host 侧 OTA，见下）。

## 从机固件升级：优先用 host 侧 OTA

ESP-Hosted 支持由 **host 经现有传输通道**给从机刷固件（`esp_hosted_slave_ota_*`）：
把从机镜像写进 host 的一个数据分区，host 启动后分段推给从机。
实测（P4 + C6）：1.28 MB 镜像约 9 秒传完并生效 ✓，**不需要任何额外接线或工具**。
只有在从机完全无法通信（或 OTA 不受支持）时才回到 UART 刷写。

## 边界与陷阱

- 大版本升级会**重构 Kconfig 符号**（例如 2.x 的内存/总线开关在 3.x 里消失）⇒
  升级后要重新核对配置项是否还存在、是否仍生效，别只看 `sdkconfig` 里还留着旧行。
- host 组件与从机固件的**最低 IDF 版本**要求会变（3.0.9 要求 IDF ≥ 5.5）。
- "host 版本更新"不等于"从机也要马上刷"：新 host 往往对旧从机有兼容路径
  （会打印 `compatible streaming mode enabled`），但**兼容路径不代表性能最优**。
- 结论只对"这一对版本组合 + 这块板"负责：换板、换从机芯片、换 IDF 大版本都要重测。

## 相关

- 项目内记录（含完整单变量实验与配方）：
  `pico_dm_qd3503728_esp32p4_idf/notes/wifi-over-c6-hosted.md`
