# Wayland 桌面采集（xdg-desktop-portal → PipeWire → gst）

> **采集真桌面的关键不是管道，是授权框选对哪一项。** KDE 门户的授权框有**三项**，
> 必须选带**你屏幕名字**的那条（`Share "<monitor>"`，列表里第一条、最大的那个，容易被
> 当成提示文字）。选 `Share virtual screen` 会得到一块**新建的空屏幕**：投屏"成功"、
> 设备侧照样 `0 bad`，但画面上永远只是那台空屏幕的壁纸 ✗。

**TL;DR —— 三条硬规则**

1. **选真显示器**（`source_type: 1`），不要 `virtual screen`（`source_type: 4`）。
2. **先把源钉成"节点声明过的 caps"再转换**：`pipewiresrc` 的 src 模板 caps 是 `ANY`，
   下游要什么它就把**源**fixate 成什么；门户节点是固定尺寸/固定像素格式的，直接要
   `I420 480x320` 会以 `no more input formats` / `set output format: -22` 整条管道死 ✗。
   尺寸**必须**钉死（否则下游的 480x320 会漏回源），格式要**宽容**（声明过的都收）。
3. **判据看帧内容**，不看设备侧计数：虚拟屏那批帧设备侧同样 `0 bad`。

## 1. 授权框三项与它们给出的流（KDE / xdg-desktop-portal-kde，实测）

| 对话框选项 | 门户流属性 | 实际内容 |
|---|---|---|
| `Share "<monitor name>"`（第一项，最大那条） | `source_type: 1`，`mapping_id` = 显示器名（如 `eDP-1`），size = 逻辑尺寸 | **真桌面** ✓ |
| `Share virtual screen (create a virtual screen, then share)` | `source_type: 4`，`mapping_id: Virtual-virtual-xdp-kde-` | **新建的空屏幕**，只有壁纸：6 张帧 md5 全同、帧间差异 `(0,0)`、11.6 KB/帧 ✗ |
| `Share region (crops a specific area)` | `source_type: 1`，size = 框选区域 | 真桌面的一个矩形区域 ✓ |

- 门户报的是**逻辑尺寸**，节点缓冲区是**物理像素**：屏幕 125% 缩放时框选 `727x489`
  ⇒ 节点声明 `909x611`（×1.25）。**别拿门户给的尺寸去钉 capsfilter**（实测 `size-portal` 失败 ✗）。
- `source_type`：`1=MONITOR 2=WINDOW 4=VIRTUAL`。
- 三次异常现象同一根因：第一轮"暗灰 flat 帧"（mean 35 / std 0.65）、随后两轮"两次运行
  的帧字节完全相同"，都是**虚拟屏的静止壁纸**。

## 2. 可用管道（面板上是真桌面 mirror）

```text
pipewiresrc fd=<fd> <target> ! <pin> ! videoconvert ! videoscale
  ! videorate drop-only=true
  ! video/x-raw,format=I420,width=480,height=320,framerate=30/1
  ! jpegenc quality=75 ! fdsink fd=1 sync=true
```

- `<pin>` = 节点声明变体的**并集**，例：`video/x-raw,format={BGRA,BGRx},width=1920,height=1080`。
- **不要钉帧率**：节点是 `framerate=0/1` + `max-framerate=60/1`，钉 `10/1` 实测直接
  `no more input formats` ✗；帧率交给下游 `videorate`。
- `drop-only=true`：只丢不复制。静止画面本来不该重复发同一帧（面板保持上一帧）；
  未加时静止画面被复制成 43 张**同一张**、灌出 12.75 MB/s 去冲一条 5 MB/s 的链路 ✗。
- `fdsink` 的 `sync` **默认就是 false**（已查 `gst-inspect`）；不跟时钟走就会自由狂奔。

## 3. 症状 → 原因（全部实测）

| 现象 | 原因 |
|---|---|
| `stream error: no more input formats` | 客户端要的 caps 不在节点 `EnumFormat` 列表里（格式/尺寸/帧率任一不符） |
| `stream error: set output format: -22` | 同样是让源去**改**门户节点；虚拟屏接受改写，真显示器/区域不接受 |
| `not-negotiated (-4)`，跑了一段后死 | 门户中途切到**它自己声明过的另一个变体**（BGRA↔BGRx），只钉一种就拒绝 ⇒ 用并集 |
| `stream error: target not found` | 连接目标解析失败。`path=<数字 id>` 在**流跑到一半重新解析时**会失效 ⇒ 优先 `target-object=<object.serial>` / `<media.name>`，把 `path=` 放最后 |

## 4. 定位方法：一次授权跑完的单变量扫描

每跑一次采集都要人在桌面上点一次授权框 ⇒ 一次只试一个管道太贵。做法：
**同一个 portal 会话里反复 `OpenPipeWireRemote` 拿多条新 fd**，对同一个节点跑 N 个管道，
总共只弹一次框；每个管道带 `-v`，失败时也能看到它到底协商成了什么。
工具：本仓 `wireless/p4_wireless_display/tools/portal_probe.py`（只产事实）。

一次定案的例子：`bare`（不约束任何 caps）报 `target not found`、`convert`（只加转换器）✓、
`format-i420` / `rate-10` / `size-480x320` / `size-portal` / `full-old` 全
`no more input formats`、`node-fix`（钉节点声明的 caps）✓ —— 于是"必须钉源，且只能钉
声明过的值"这条结论就成立了。

## 5. 实测（真显示器 · 画面在动 · 确认测量）

门户流：`mapping_id: eDP-1`、`size (1536, 864)`、`source_type: 1`（逻辑尺寸；节点声明
`BGRA/BGRx 1920x1080`，即 125% 缩放后的物理像素）。

| 指标 | 值 |
|---|---|
| 稳定性 | 38.81 s 内 **0 次中途退出、0 次重启** ✓ |
| 连接标识 | `target-object=<object.serial>` **第一轮就成功，之后再没换写法** ✓ |
| 帧率 | 平均 **21.1 fps / 0.53 MB/s**（818 帧）；**随画面变化**：静止 0.8~2 fps，拖动/播放时 36~72 fps |
| 帧内容 | 5 千~2.8 万唯一色、亮度 std ~30、相邻帧像素差 90~235 ⇒ 真桌面且在动 ✓ |

**观察（不是缺陷）**：逐秒窗口出现过 **72.4 fps**，超过钉住的 30 fps 上限 ——
`videorate` 的 `30/1` 是**流时间**上的上限，墙钟上追赶时允许突发。峰值 ≈72 fps × 26 KB
≈ **1.8 MB/s**，仍在链路 4.95~5.03 MB/s 预算内，所以不改。

对照（同一块板、静止桌面）：73 帧 / 40 s ≈ 1.8 fps ⇒ **引用帧率必须说明画面有没有在动**。

## 6. 测量方法学（两次假读数，都是脚本自己造的）

- **别在测量路径里盲等**：脚本里"先等 2 秒确认起来了"会让子进程写满管道而阻塞，攒下的帧
  在开始读时一次涌出 ⇒ 算出 `17 帧 / 0.03 s = 538 fps`、`12.75 MB/s` ✗
  （真实是那 2 秒里 ~8.5 fps）。正确做法：**一启动就读 stdout**，用"进程自己退没退"判失败。
- **判据必须能识别"管道已经死了"**：一次 42 帧的突发让 fps 算出 1222，脚本报 `PASS`，
  而管道其实 1.4 秒后就 `not-negotiated` 挂掉 ✗。现在判据要求"采集进程全程没自己退出"。

## 未验证 / 待办

- `target-object` 用 `object.serial` 还是 `media.name` 更稳：**没有 A/B**。已知的是
  `path=<数字 id>` 在一次完整运行里跑到一半重新解析失败（`target not found`），
  而 `target-object=<serial>` 在确认测量里一轮到底没换过。当前实现按
  serial → name → 数字 id 轮换，属于"两个都留着"。
- 触摸/输入回传、UDP 换 TCP 的延迟对比：与本篇无关，见 `wifi-over-c6-hosted.md`。
- H.264 / Moonlight 路线**前提不成立**：P4 只有 H.264 **编码器**、没有解码器
  （datasheet §4.2.1.5，全文 0 处 decoder）⇒ 客户端没法解 Moonlight 的码流。
