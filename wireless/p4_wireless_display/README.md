# p4_wireless_display —— 无线显示器

> PC 经 WiFi 推 JPEG → P4 **硬件解码** → i80 面板上屏。实测 **60 fps、0 丢帧**
> （解码 1.70 ms/帧、上屏 3.95 ms/帧），带宽只用了链路的 11%。

本工程在 `pico_dm_qd3503728_esp32p4_idf/wireless/` 下（与设备端固件同仓）。
**知识库在 `../../notes/`**：PC 侧采集看 `../../notes/wayland-portal-capture.md`，
链路与 hosted 配置看 `../../notes/wifi-over-c6-hosted.md`，链路证据在
`../p4_wifi_probe/FINDINGS.md`。

## 用法（三个用户脚本）

先记住设备地址（一次就够；也可以每次 `--host <IP>` 或 `export PUD_HOST=<IP>`）：

```bash
tools/pud_image.py --host <设备IP> --save-host
```

```bash
tools/pud_cast.py                    # 投屏：桌面实时镜像（会弹一次系统授权框）
tools/pud_video.py ~/Videos/clip.mp4  # 放视频（默认循环，Ctrl-C 停）
tools/pud_image.py photo.jpg          # 显示一张图
tools/pud_image.py ~/Pictures/ --seconds 10   # 目录轮播
```

- **投屏时授权框选带屏幕名字的那一项**（`Share "<显示器>"`，第一条、最大的那个）：
  选 `virtual screen` 会投一块新建的空屏幕 —— 看起来"成功"，但画面永远不是你的桌面。
- 停止一律 Ctrl-C。设备侧只画面上有变化才收到新帧（静止画面重复发同一张纯属浪费链路），
  所以投屏时面板静止不动是**正常的**。
- 图片/视频一律**保持比例加黑边**缩放到 480x320（不拉扁）。
- 退出码：0 成功 / 2 用法错 / 3 环境问题（设备不通、缺 ffmpeg/gst/gi）。

## 目录结构

```text
tools/     库 + 工具 + 用户脚本（都在 sys.path 上，互相 import）
  pudnet.py         帧传输库：UDP 分片头 + 丢帧策略 + TCP 变体 + 连通性预检
  portal_capture.py 门户采集库：授权 / caps 发现 / 采集管道 / 重启监督
  pud_media.py      ffmpeg 媒体源：视频 / 图片 / 合成测试图 → 480x320 JPEG
  pudcli.py         用户脚本公共外壳：设备地址、进度、退出码
  pud_cast.py       ★ 用户脚本：投屏
  pud_video.py      ★ 用户脚本：放视频
  pud_image.py      ★ 用户脚本：看图 / 轮播
  portal_probe.py   工具：门户采集单变量扫描（只产事实，画面不对时用它定位）
tests/     验证脚本（oracle + 统一退出码，见 tests/README.md）
main/      设备端固件（与仓库根的 main/ 显示层同步）
```

## 设备端：构建与烧写

```bash
. ~/esp/esp-idf-v6.1/export.sh
idf.py build
idf.py -p /dev/ttyACM0 flash monitor   # 只看日志加 --no-reset，否则会复位板子
```

**传输二选一**（`menuconfig → Wireless display demo → Frame transport`）：
**UDP + 丢帧（默认）** 或 **TCP 流（回退）**（`pudnet.Sender` / `pudnet.TcpSender`）。

设备 IP 在启动日志里（`wdd: got IP …`）。WiFi 凭据只放在**被 gitignore 的
`sdkconfig`**（`CONFIG_WDD_WIFI_SSID` / `CONFIG_WDD_WIFI_PASSWORD`）。

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
- 桌面投屏（`pud_cast.py`）：818 帧 / 38.81 s = 21.1 fps / 0.53 MB/s、0 次中断 ——
  **帧率取决于屏幕变化量**（静止 0.8~2 fps、拖动/播放时 36~72 fps），静止时的低帧率是预期行为。

**测量陷阱（踩过）**：UDP 的 `send()` 不阻塞 ⇒ 不限速时 PC 会以 80 MB/s 往核心里灌包、
绝大多数在本地就丢了，PC 侧 fps 变成"往内核塞包的速度"、**毫无意义** ✗。
限速在发送侧做（`pudcli.run_stream` 的 `pace`），判据一律用**设备侧的 shown/dropped**。
另外 `ffmpeg -re` 也不守时（实测 3.0 s 的片子 2.46 s 吐完 ✗），所以视频播放的限速同样在发送侧。

## 设计要点（都是前面几轮踩出来的）

- **解码缓冲必须在 PSRAM**（`esp_driver_jpeg` 的 2D-DMA 约束），而 i80 **能直接读
  PSRAM** ⇒ 解码结果零拷贝上屏，不占内部 RAM；
- **双缓冲乒乓**：解第 N+1 帧时第 N 帧还在 DMA 传输（`tft_async_video_flush/wait`）；
- **收帧缓冲也在 PSRAM 且 16 字节对齐**（用 `jpeg_alloc_decoder_mem`），可以直接收进
  解码器输入缓冲；
- **只在收帧任务里解码**，绝不放网络回调（USB 那轮的教训）；
- 帧头极简：`[u32 frame][u16 idx][u16 cnt][u32 total] + payload`，**设备一见到更新的
  帧号就丢弃未收全的旧帧** ⇒ 丢分片只丢一帧、不会卡住画面。

## 约束与注意

- **host 组件必须 `espressif/esp_hosted "~3"`**：IDF 官方例子给 P4 钉的 `"~2"` 在
  本板上数据面直接崩（`0x102` → `Unrecoverable host sdio state`）。别"顺手升级/降级"。
- **`CONFIG_LWIP_UDP_RECVMBOX_SIZE` 必须是 64**（默认 6）：采集源成串出帧，突发会把默认
  邮箱打爆 ⇒ 丢分片 ⇒ 整帧被丢掉（实测只显示 9.4/30 fps ✗）；改 64 后恢复 ✓。
- **ffmpeg 的 MJPEG 必须加 `-pix_fmt yuvj420p`**：默认采样布局非标准，P4 硬件解码器不认
  （`Sampling factor cannot be recognized` ✗）。`pud_media.py` 已经带上了。
- `main/panel.c`、`main/tft.h`、`main/board_pins.h` 是**从同仓固件的 `main/` 照抄**的
  （那一版已上板验收）。改显示层要两边同步：`diff main/panel.c ../../main/panel.c`。
- 演示只做 480×320 全屏整帧刷；分辨率/旋转改了要同步 `board_pins.h` 与 `pudnet.FRAME_W/H`。

## 诊断与测试

```bash
python3 tools/portal_probe.py                 # 门户采集单变量扫描（一次授权跑完，只出事实）
python3 tools/portal_probe.py --list          # 看看有哪些实验项
python3 tests/test_display_stream.py --host <设备IP>   # 端到端速率（PASS/FAIL，带 oracle）
```

**判据不要只看设备侧计数**：`0 bad` 只说明解码没报错 —— 投虚拟屏那批帧同样 `0 bad`。
看**帧内容**（唯一色数、亮度 std、相邻帧像素差异）：`pud_cast.py --save-frames DIR`
会把最后几张 JPEG 落盘。完整机制与症状对照见 `../../notes/wayland-portal-capture.md`。

## 还没做

- `target-object` 用 serial 还是 name 更稳：缺 A/B 证据（现在按 serial → name → 数字 id 轮换）。
- **H.264 / Moonlight 路线不成立**：P4 只有 H.264 编码器、没有解码器。
