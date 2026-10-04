# C6（协处理器）固件镜像

两套镜像各四个文件（app + bootloader + 分区表 + ota_data），**当前板上跑的是 3.0.9**。

| 文件 | 版本 | 说明 |
|---|---|---|
| `eh_cp_3.0.9.bin`（+ `*_3.0.9.bin`）| **3.0.9** | 与 host 组件同版本 ⇒ 日志出现 `fw versions: host=3.0.9 coprocessor=3.0.9 (match)` 且 **SDIO SW_AGGR 协商成功**。当前使用 ✓ |
| `network_adapter_2.12.13.bin`（+ `*_2.12.13.bin`）| 2.12.13 | 旧版；配 host 3.x 时只能走兼容模式（`CP without SDIO SW_AGGR`）。留作回退 |

## 怎么编（离线也能编）

CP 固件工程在组件里：`managed_components/espressif__esp_hosted/examples/ota/coprocessor_ota/cp/`。

```bash
idf.py -C <cp 工程> -B <builddir> set-target esp32c6
idf.py -C <cp 工程> -B <builddir> build      # 产物：eh_cp_ota_coprocessor_ota.bin
```

**坑（实测）**：该工程的 `main/idf_component.yml` 默认从组件仓库拉
`espressif/esp_hosted`；本机访问不到 `components-file.espressif.com` 时会直接配置失败 ✗。
改成指向本地组件即可，但 `override_path` **必须写绝对路径**（相对路径解析基准与文档所述
不一致，实测失败）。

## 怎么刷（两条路，都已实测）

**路线 A：主机侧 OTA（首选，零接线）** —— 用 `../`（host_performs_slave_ota 工程）的
partition 法：把 app 镜像写进主机 `slave_fw` 分区，主机启动后经 SDIO 推给 C6，
从机侧 A/B OTA 写入并自动 `activate` 重启（从机 ≥ 2.6 才支持 activate）。

```bash
python -m esptool -p <port> --chip esp32p4 -b 460800 write-flash --force 0x5F0000 eh_cp_3.0.9.bin
```

实测：1 175 552 B / 约 8 s 传完 ⇒ `New firmware activated - slave will reboot` ✓

**路线 B：H4 排针 + USB-TTL（恢复用）** —— H4 = `1:IO9 2:GND 3:C6_U0RXD 4:C6_U0TXD`；
TTL 的 TX→3、RX→4、GND→2；**先给 IO9 短到 GND，按住板子 BOOT 再上电**，然后：

```bash
python -m esptool -p <port> --chip esp32c6 -b 460800 write-flash --flash-mode dio \
  --flash-size 4MB --flash-freq 80m \
  0x0 bootloader_3.0.9.bin 0x8000 partition-table_3.0.9.bin 0xd000 ota_data_initial_3.0.9.bin 0x10000 eh_cp_3.0.9.bin
```

依据与实测见 `../../../notes/wifi-over-c6-hosted.md`。
