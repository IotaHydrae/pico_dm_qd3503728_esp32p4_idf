# p4_wifi_probe —— P4 + 片内 C6 链路吞吐探针

> 只回答一个问题：**从主机经 WiFi 推数据到 P4，能跑多快、稳不稳**。
> 答案：**约 5 MB/s（40 Mbps）**，ping RTT 平均 4.5 ms。从机 3.0.9 连测三次
> **4.95 / 5.01 / 5.03 MB/s**，从机 2.12.13 兼容模式单次 60 s 长跑 **5.44 MB/s** ——
> 两组测量条件不同，**没有做 A/B**，引用时请连条件一起引。

完整证据链、单变量实验与排除清单在 **[FINDINGS.md](FINDINGS.md)**（含那条最重要的教训：
IDF 官方 iperf 例子给 P4 钉的 `esp_hosted "~2"` 在本板数据面直接崩，必须 `~3`）。

## 怎么跑

```bash
. ~/esp/esp-idf-v6.1/export.sh
idf.py build
idf.py -p /dev/ttyACM0 flash
idf.py -p /dev/ttyACM0 monitor --no-reset     # 不加 --no-reset 会复位板子

# PC 侧打流（用系统 python）
/usr/bin/python3 net/throughput.py <设备IP> --seconds 10
```

设备启动后会在串口打印自己的 IP 与 `TCP sink listening on port 5001`。
判据（写死在脚本里）：**≥1 MB/s（8 Mbps）算 PASS** —— 480x320 JPEG q60 @60fps 只需 0.66 MB/s。

## 这是什么、不是什么

- 是：链路能力测量 + 一份可复现的 hosted 配置配方（见 FINDINGS 的"复现配方"）。
- 不是：显示应用。要画面请看同级的 `../p4_wireless_display/`。

WiFi 凭据只放在被 gitignore 的 `sdkconfig`（`CONFIG_PROBE_WIFI_SSID/PASSWORD`）。
