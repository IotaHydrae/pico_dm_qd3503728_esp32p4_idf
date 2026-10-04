# p4_wireless_display —— 无线显示器（最小端到端 demo）

> PC 经 WiFi 推 JPEG → P4 **硬件解码** → i80 面板上屏。实测 **60 fps、0 丢帧**
> （解码 1.70 ms/帧、上屏 3.95 ms/帧），带宽只用了链路的 11%。

本工程在 `pico_dm_qd3503728_esp32p4_idf/wireless/` 下（与设备端固件同仓）。知识库在
`../../notes/`（**PC 侧采集看 `../../notes/wayland-portal-capture.md`**，
链路与 hosted 配置看 `../../notes/wifi-over-c6-hosted.md`），
链路调试的完整证据在 `../p4_wifi_probe/FINDINGS.md`。

## 怎么跑

```bash
# 1) 构建并烧写 P4 端（本板 USB 转串口接了 DTR/RTS，不用按键）
. ~/esp/esp-idf-v6.1/export.sh
idf.py build
idf.py -p /dev/ttyACM0 flash monitor        # 只看日志加 --no-reset，否则会复位板子

# 2) PC 端推帧（用系统 python，别用 IDF 的 python 环境——那里没有 PIL）
/usr/bin/python3 net/send_frames.py <设备IP> --udp --fps 120 --seconds 30   # UDP（默认传输）
/usr/bin/python3 net/send_frames.py <设备IP>         --fps 60  --seconds 30  # TCP 回退
```

**传输二选一**（`menuconfig → Wireless display demo → Frame transport`）：
**UDP + 丢帧（默认）** 或 **TCP 流（回退）**。

设备 IP 在启动日志里（`wdd: got IP …`）。WiFi 凭据只放在**被 gitignore 的
`sdkconfig`**（`CONFIG_WDD_WIFI_SSID` / `CONFIG_WDD_WIFI_PASSWORD`）。

## 画面上应该看到什么（判据）

移动的三条彩色竖条（1 秒一个来回）+ 大号帧号 + 秒表：
竖条平滑左移 = 60 fps 生效；帧号连续 = 无丢帧；秒表与 PC 时间差几十毫秒 = 端到端延迟正常。

## 实测

| 场景 | 端到端 fps | 丢弃 | 瓶颈 |
|---|---|---|---|
| **UDP 60 fps**（工作点）| 59.6 | 18 帧（仅启动爬坡）| 主机节流 |
| **UDP 120 fps × 20 s** | **118.5** | ~1 帧/秒（≈1%）| 接近设备上限 |
| **UDP 150 fps 目标** | **145~147** | ~2~4% | 接近设备上限 |
| UDP 175/200 fps 目标 | 130 / 30（崩） | 猛增 | **设备流水线到顶**（176 fps）|
| TCP 预编码猛推 | 114.8 | 0 | TCP 往返/窗口 |
| 设备内部上限（decode+flush）| ~176 | — | decode 1.69 ms + flush 3.96 ms |

- **UDP + 丢帧把上限从 114 fps 提到 ~150 fps**：TCP 受往返/窗口限制，UDP 没有这个约束。
- 到 ~175 fps 时崩溃点正好等于设备流水线上限（5.67 ms/帧）⇒ 再快也没意义。
- 全过程 **0 bad**：残帧从不喂给解码器（收到更新的帧号就直接丢弃未组装完的帧）。
- 链路本身 4.95~5.03 MB/s（40 Mbps），见 `../p4_wifi_probe/FINDINGS.md`。

**测量陷阱（踩过）**：UDP 的 `send()` 不阻塞 ⇒ `--udp --fps 0` 时 PC 会以 80 MB/s 往核心里灌包、
绝大多数在本地就丢了，PC 侧 fps 变成"往内核塞包的速度"、**毫无意义** ✗。
脚本已加保护（UDP 必须限速），判据一律用**设备侧的 shown/dropped**。

## 设计要点（都是前面几轮踩出来的）

- **解码缓冲必须在 PSRAM**（`esp_driver_jpeg` 的 2D-DMA 约束），而 i80 **能直接读
  PSRAM** ⇒ 解码结果零拷贝上屏，不占内部 RAM；
- **双缓冲乒乓**：解第 N+1 帧时第 N 帧还在 DMA 传输（`tft_async_video_flush/wait`）；
- **收帧缓冲也在 PSRAM 且 16 字节对齐**（用 `jpeg_alloc_decoder_mem`），可以直接收进
  解码器输入缓冲；
- **只在收帧任务里解码**，绝不放网络回调（USB 那轮的教训）；
- 帧格式故意极简：`[u32 小端长度][JPEG]`。

## 约束与注意

- **host 组件必须 `espressif/esp_hosted "~3"`**：IDF 官方例子给 P4 钉的 `"~2"` 在
  本板上数据面直接崩（`0x102` → `Unrecoverable host sdio state`）。别"顺手升级/降级"。
- `main/panel.c`、`main/tft.h`、`main/board_pins.h` 是**从同仓固件的 `main/` 照抄**的
  （那一版已上板验收）。改显示层要两边同步：`diff main/panel.c ../../main/panel.c`。
- 演示只做 480×320 全屏整帧刷；分辨率/旋转改了要同步 `board_pins.h` 与
  `FRAME_W/H`。

## 实时采集投屏（`net/send_screen.py`）

```bash
/usr/bin/python3 net/send_screen.py <设备IP> --source testsrc --fps 30 --seconds 30     # 合成源，无需权限
/usr/bin/python3 net/send_screen.py <设备IP> --source video=/path/clip.mp4 --fps 30     # 视频文件
/usr/bin/python3 net/send_screen.py <设备IP> --source portal --fps 30                   # Wayland 桌面（WIP）
```

实测（`testsrc` 30 fps × 12 s）：设备侧 **30.0 fps shown、丢弃停在 6（仅启动）、0 bad** ✓。

### 两个必须记住的坑（都实测踩过）

1. **ffmpeg 的 MJPEG 必须加 `-pix_fmt yuvj420p`**：默认输出是三个分量都 (h=1,v=2) 的非标准
   采样布局，P4 硬件解码器不认（`Sampling factor cannot be recognized` ✗，帧全被记成 bad）。
   标准 4:2:0 才与已验证可解的 PIL `subsampling=2` 一致 ✓。
2. **`CONFIG_LWIP_UDP_RECVMBOX_SIZE` 必须是 64**（默认 6）：采集源是"成串出帧"的，
   突发会把默认邮箱打爆 ⇒ 丢分片 ⇒ 整帧被丢帧策略丢掉，设备只显示出 **9.4/30 fps** ✗；
   改 64 后恢复 30/30 ✓（官方 P4+hosted 配置用的也是 64）。

### Wayland 桌面采集：**已打通** ✓（面板上是真桌面 mirror）

```bash
/usr/bin/python3 net/send_screen.py <设备IP> --source portal --seconds 40 \
  --save-frames /tmp/wd_frames --min-fps 0
```

链路：`门户授权 → PipeWire → pipewiresrc →（钉源 caps）→ 缩放/转码 → jpeg → UDP → 设备`。

**授权框有三项，必须选带屏幕名字的那条**（`Share "<monitor>"`，第一条、最大的那个，
容易被当成提示文字）：

| 选项 | 门户流 | 内容 |
|---|---|---|
| `Share "<monitor>"` | `source_type: 1` | **真桌面** ✓（实测 73 帧、相邻帧像素差最大 203、~36 KB/帧） |
| `Share virtual screen` | `source_type: 4`，`mapping_id: Virtual-…` | 一块**新建的空屏幕**，只有壁纸：6 帧 md5 全同、帧间差 `(0,0)`、11.6 KB/帧 ✗ |
| `Share region` | `source_type: 1` + 裁剪尺寸 | 真桌面的一个矩形区域 ✓ |

选 virtual screen 时投屏照样"成功"、设备侧照样 `0 bad` —— 只是画面永远不是你的桌面。
**完整机制/症状对照/定位方法见 `../../notes/wayland-portal-capture.md`。**

关键坑（都实测过）：

1. **必须把源钉成"节点声明过的 caps"，不能只钉下游**：`pipewiresrc` 的 src 模板 caps 是
   `ANY` ⇒ 下游要 `I420 480x320` 它就把**源**fixate 成那套、并让门户去改自己的格式
   ⇒ `no more input formats` / `set output format: -22` 整条死 ✗。正确形状：
   `pipewiresrc ! video/x-raw,format={BGRA,BGRx},width=<节点尺寸> ! videoconvert ! videoscale …`
   —— 尺寸钉死、格式取并集、**帧率不钉**（节点是 `0/1`，钉了就死）。
2. **`drop-only=true` 且 `fdsink sync=true`**：`fdsink` 的 `sync` 默认 false，不跟时钟会
   自由狂奔；不加 `drop-only` 时静止画面会被复制成同一张反复发（实测 43 张塞进 30 ms、
   12.75 MB/s 冲一条 5 MB/s 的链路 ✗）。
3. **连接标识按可靠性排序**：`target-object=<object.serial>` → `<media.name>` →
   `path=<数字 id>`。实测数字 id 会在流跑到一半重新解析失败（`target not found`），
   管道自己死掉，`--max-attempts` 会自动换写法重起。
4. portal 的 `Response` 可能先于方法回复到达 ⇒ **必须先订阅再调用**（否则永远卡住、连授权框都不弹）。
5. `GLib.Variant("(a{sv})", (dict,))` 必须是**单元素元组**，传裸字典会报
   `Dictionary entries must have two elements {sv} s`。
6. fd 只能由 **`OpenPipeWireRemote`**（方法回复里带 fd）取得，信号回调拿不到；
   同一个会话可以**反复取 fd**，这是"一次授权跑 N 个候选管道"（`net/probe_portal.py`）的基础。

### 判据（不要只看设备侧计数）

设备侧的 `0 bad` 只说明解码没报错 —— 虚拟屏那批帧同样 `0 bad`。看**帧内容**：

- `--save-frames DIR`：同一路媒体 `tee` 一份存盘（`multifilesink`，只留最后 N 张）；
- 判据：唯一色数、亮度 `std`、以及**相邻帧之间的像素差异**（全 0 ⇒ 静止壁纸，
  真桌面 ≥ 上百）；
- `--min-fps 0` 只验"链路活着、管道没死" —— 因为 `drop-only` 下**帧率取决于屏幕变化量**，
  静止画面不出帧是预期行为（实测静止桌面 ~1.8 fps，73 帧/40 s）。

### 确认测量（真显示器 · 画面在动）

818 帧 / 38.81 s = **21.1 fps / 0.53 MB/s**，0 次中途退出、0 次重启；
`target-object=<object.serial>` 第一轮就成功且再没换写法。
帧率**随画面变化**：静止 0.8~2 fps、拖动/播放时 36~72 fps —— 逐秒出现 72.4 fps 是因为
`videorate` 的 `30/1` 是**流时间**上限，墙钟追赶允许突发（峰值 ≈1.8 MB/s，仍在链路预算内）。

### 还没做

- `target-object` 用 serial 还是 name 更稳：缺 A/B 证据（现在按 serial → name → 数字 id 轮换）。
- **H.264 / Moonlight 路线不成立**：P4 只有 H.264 编码器、没有解码器。
