#!/usr/bin/env python3
"""PC 侧实时采集 → JPEG → 推给 P4 无线显示器（UDP 分片，与 send_frames.py 同格式）。

    # ORACLE: REQUIREMENT
    # SOURCE: 链路 4.95~5.03 MB/s；设备端 60 fps 只需 0.66 MB/s，150 fps 约 1.16 MB/s
    # EXPECTED: >= 30 fps 端到端（设备侧 shown 判据），带宽 < 2 MB/s

采集源（`--source`）：
  portal   Wayland 桌面（xdg-desktop-portal ScreenCast → PipeWire → pipewiresrc）。
           **首次会弹权限对话框**：选"显示器/区域"那类选项，可以像截图一样划一块矩形。
           采集管道不是写死的：先用一次短管道**探出节点真正提供的 caps**（格式 + 缓冲区
           尺寸），把它钉在源上，再 videoconvert/videoscale/videorate 转成 480x320 I420。
           不这么做的话下游 caps 会把源改写掉 ⇒ `no more input formats` / `-22` ✗。
  video=<file>  视频文件（ffmpeg 解码 → 缩放 → JPEG）——不需要任何权限，用来验证管线。
  testsrc  ffmpeg 合成测试图（同上，永远可用）。

退出码（与工作区 testing skill 一致）：
    0 PASS / 1 FAIL / 2 INVALID_USAGE / 3 ENVIRONMENT_ERROR / 4 TIMEOUT

设计说明（为什么这么绕）：Wayland 下没有 X11 的 "抓 root window" 这条路（实测 XWayland
的 root 是全黑 ✗），屏幕内容只能经 portal 授权后由 PipeWire 提供；而 portal 给的 PipeWire
连接是**一个 fd**，必须由能收 fd 的客户端（这里用 PyGObject 的
`call_with_unix_fd_list_sync`）拿到，再把它交给 `pipewiresrc fd=<n>`。
"""

import argparse
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time

W, H = 480, 320
UDP_PAYLOAD = 1400
HDR = struct.Struct("<IHHI")          # 与设备端 udp_frag_hdr_t 一致
PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SC = "org.freedesktop.portal.ScreenCast"
REQ = "org.freedesktop.portal.Request"

# gst-launch -v 打印的 pipewiresrc 实际协商结果（协商在 PAUSED 阶段就完成，早于任何 buffer）
SRC_CAPS_RE = re.compile(r"pipewiresrc0\.GstPad:src: caps = (.+)")
CAPS_FIELDS = ("format", "width", "height", "framerate")
CAPS_FIELD_RE = {f: re.compile(rf"\b{f}=\((?:string|int|fraction)\)([^,]+)")
                 for f in CAPS_FIELDS}


def node_enum_formats(params):
    """从 pw-dump 的节点 params 里抽 EnumFormat，返回 [{width,height,formats,dma_buf}]。

    真实 schema（实测）：`info.params` 是**按参数名索引的字典**（`{"EnumFormat": [...]}`），
    也可能是 `None`；`info` 本身在某些对象上不存在。EnumFormat 每一项形如
    `{"size":{"width":825,"height":465},"format":{"default":"BGRA","alt1":"BGRA"}}`。
    """
    if not isinstance(params, dict):
        return []
    out = []
    for entry in params.get("EnumFormat") or []:
        if not isinstance(entry, dict):
            continue
        size = entry.get("size") or {}
        fmt = entry.get("format") or {}
        formats = [v for v in (fmt.get("default"), fmt.get("alt1"), fmt.get("alt2"))
                   if isinstance(v, str)]
        out.append({"width": size.get("width"), "height": size.get("height"),
                    "formats": formats, "dma_buf": "modifier" in entry})
    return out


def read_node_info(node, timeout_s=20):
    """用 `pw-dump` 读节点的 props + EnumFormat；读不到就返回 {}（不猜）。

    用 pw-dump 而不是再起一条 gst 探测管道，是因为它**不占 PipeWire 连接**：实测在一条
    探测连接刚结束之后紧接着起的采集管道会报 `no more input formats`，而同样的管道形状
    在每次连接都干净退出的扫描里 7/7 全过 —— 少一条连接就少一个变量。
    """
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True,
                             timeout=timeout_s).stdout
        for obj in json.loads(out):
            if obj.get("id") == node:
                info = obj.get("info") or {}
                return {"props": info.get("props") or {},
                        "enum_formats": node_enum_formats(info.get("params"))}
    except Exception:
        return {}
    return {}


def target_variants(node, info):
    """连接目标的写法，**按可靠性排序**：object.serial → media.name → 数字 id。

    `path=<数字 id>` 实测会**在流跑到一半时**重新解析失败（`stream error: target not found`，
    那时帧已经出过一批了）⇒ 优先用更稳的标识。
    """
    props = (info or {}).get("props") or {}
    variants = [f"target-object={props[key]}" for key in ("object.serial", "media.name")
                if props.get(key)]
    variants.append(f"path={node}")
    return variants


def pin_from_declared(formats):
    """把节点声明的**全部变体**拼成一条"尺寸固定、格式宽容"的 capsfilter。

    尺寸必须固定：一旦留空，下游的 480x320 会反向传到源上（实测 `no more input formats` ✗）。
    格式必须宽容：实测严格只钉一种格式时，流跑约 43 帧后门户会切到它自己声明过的另一个
    变体（BGRA ↔ BGRx）⇒ capsfilter 拒绝 ⇒ `not-negotiated` 整条管道死 ✗。
    """
    fmts, widths, heights = [], set(), set()
    for e in formats:
        if not e.get("width") or not e.get("height"):
            continue
        for f in e["formats"]:        # 逐条加：节点自己的 default/alt1 常是同一个值，要去重
            if f not in fmts:
                fmts.append(f)
        widths.add(e["width"])
        heights.add(e["height"])
    if not (fmts and widths and heights):
        return None

    def one_or_set(values):
        values = sorted(values)
        return str(values[0]) if len(values) == 1 else "{" + ",".join(map(str, values)) + "}"

    return (f"video/x-raw,format={one_or_set(fmts)},"
            f"width={one_or_set(widths)},height={one_or_set(heights)}")


def parse_src_caps(log_text):
    """从 gst-launch 日志里取 pipewiresrc 协商到的 caps 字段；没有就返回 None。

    返回 {"raw": 原始 caps 字符串, "format"/"width"/"height"/"framerate": 值}。
    """
    m = SRC_CAPS_RE.search(log_text or "")
    if not m:
        return None
    fields = {"raw": m.group(1)}
    for name, rx in CAPS_FIELD_RE.items():
        hit = rx.search(fields["raw"])
        if hit:
            fields[name] = hit.group(1).strip()
    return fields


def caps_pin(fields):
    """把协商出的 caps 拼成给源用的 capsfilter。

    **不含 framerate**：kwin 的流帧率是 0/1（不固定，只给 maxFramerate=60），钉死帧率
    会被判成 `no more input formats`（实测 rate-10 失败 ✗）。帧率交给下游 videorate。
    """
    parts = [f"{k}={fields[k]}" for k in ("format", "width", "height") if fields.get(k)]
    return "video/x-raw," + ",".join(parts)


def open_screencast_session(timeout_s=120):
    """走 portal 建立 ScreenCast 会话，返回 (bus, session_handle, streams)。

    `streams` 是门户在 Start 里给的流列表，形如 `[(node_id, {属性})]`；属性里有
    `size`（用户划的区域大小）与 `source_type`（1=MONITOR 2=WINDOW 4=VIRTUAL）。

    流程（每一步都要等 Request 对象的 Response 信号，screen 上可能弹权限框）：
      CreateSession → SelectSources(1=MONITOR) → Start
    取 fd 是**另一步**（`open_pipewire_remote`），拆开是为了能在同一个会话里反复取 fd：
    多个候选管道共用一次用户授权（见 net/probe_portal.py）。
    """
    try:
        from gi.repository import Gio, GLib
    except Exception as e:                                  # pragma: no cover
        raise RuntimeError(f"需要 PyGObject(gi)：{e}")

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def plain(v):
        """Response 里的值可能是未解包的 GLib.Variant，统一解成 Python 值。"""
        return v.unpack() if hasattr(v, "unpack") else v

    def request(method, params):
        """发起一次 portal 请求并等待它的 Response，返回 results 字典。

        **必须先订阅再调用**：CreateSession 的 Response 可能在方法回复到达之前就发出，
        后订阅会永远等不到（实测就一直卡住、桌面上连授权框都不弹 ✗）。
        做法：订阅**所有** Request.Response（object_path=None，连接是我们自己的，
        只会收到自己请求的信号），再按对象路径里的 token 匹配。
        """
        holder = {}
        loop = GLib.MainLoop()

        def on_response(_c, _s, obj_path, _i, _sig, sig_params):
            if "token" in holder and holder["token"] not in obj_path:
                return
            holder["code"], holder["results"] = sig_params.unpack()
            holder["path"] = obj_path
            if loop.is_running():
                loop.quit()

        sub = bus.signal_subscribe(PORTAL_BUS, REQ, "Response", None, None,
                                   Gio.DBusSignalFlags.NONE, on_response)
        try:
            variant = GLib.Variant(f"({params[0]})", params[1])
            reply = bus.call_sync(PORTAL_BUS, PORTAL_PATH, SC, method, variant, None,
                                  Gio.DBusCallFlags.NONE, -1, None)
            req_path = reply.unpack()[0]
            # 方法回复给出的正是这次请求的对象路径（末尾就是 token）
            holder["token"] = req_path.rsplit("/", 1)[-1]
            GLib.timeout_add_seconds(timeout_s, lambda: (loop.quit(), False)[1])
            loop.run()
        finally:
            bus.signal_unsubscribe(sub)
        if "code" not in holder:
            raise RuntimeError(f"{method} 等 Response 超时（{timeout_s}s）")
        if holder["code"] != 0:
            raise RuntimeError(f"{method} 未获授权（Response={holder['code']}，"
                               "1=用户取消 2=其它错误）")
        return {k: plain(v) for k, v in holder["results"].items()}

    # CreateSession 的第二个返回值（a{sv}）里带 session_handle
    # 注意：签名 "(a{sv})" 对应的是一个**单元素元组**，所以这里必须写 (dict,)；
    # 直接传裸字典会被当成"元组本身"去迭代 key ⇒ TypeError: Dictionary entries must
    # have two elements {sv} s（踩过 ✗）
    res = request("CreateSession", ("a{sv}", ({
        "session_handle_token": GLib.Variant("s", "wdd"),
    },)))
    session_handle = plain(res.get("session_handle"))
    if not session_handle:
        raise RuntimeError("CreateSession 没有返回 session_handle")

    # 只要显示器、不要多个、光标嵌入（1=hidden 2=embedded）
    request("SelectSources", ("oa{sv}", (session_handle, {
        "types": GLib.Variant("u", 1),
        "multiple": GLib.Variant("b", False),
        "cursor_mode": GLib.Variant("u", 2),
    })))

    # 这里会弹权限框：请在桌面上点"共享/允许"并选一个显示器
    results = request("Start", ("osa{sv}", (session_handle, "", {})))
    streams = plain(results.get("streams")) or []
    if not streams:
        raise RuntimeError("Start 没有返回流（是不是没选显示器？）")
    # 打印门户给的流属性：**哪一块屏/区域**、多大、什么源类型 —— 画面不对时先看这个
    # 尺寸就是用户在授权框里划的区域大小，所以**绝不能**在 pipewiresrc 上钉死
    # width/height：监视器流是固定尺寸的，源会以 `set output format: -22` 拒绝 ✗
    print(f"portal streams: {streams!r}", flush=True)
    return bus, session_handle, streams


def open_pipewire_remote(bus, session_handle):
    """取一个 PipeWire fd；**同一会话可反复调用**，每次得到一条独立的新连接。

    fd 只能用 OpenPipeWireRemote 取：它在方法回复里带 fd，Gio 的
    `call_with_unix_fd_list_sync` 能拿到；Response 是信号，Python 侧的 signal 回调
    拿不到消息的 fd 列表 ✗。
    """
    try:
        from gi.repository import Gio, GLib
    except Exception as e:                                  # pragma: no cover
        raise RuntimeError(f"需要 PyGObject(gi)：{e}")

    reply, fds = bus.call_with_unix_fd_list_sync(
        PORTAL_BUS, PORTAL_PATH, SC, "OpenPipeWireRemote",
        GLib.Variant("(oa{sv})", (session_handle, {})), None,
        Gio.DBusCallFlags.NONE, -1, None)
    if fds is None or fds.get_length() < 1:
        raise RuntimeError("OpenPipeWireRemote 没有返回 fd")
    # PyGObject 是 Gio.UnixFDList.get(index)（没有 get_message_fd）
    return fds.get(0)


def open_portal_screencast(timeout_s=120):
    """(pipewire_fd, node_id) —— 一次性拿 fd 的简单入口（--probe 用）。"""
    bus, session_handle, streams = open_screencast_session(timeout_s)
    return open_pipewire_remote(bus, session_handle), streams[0][0]


def discover_portal_caps(bus, session_handle, node, timeout_s=5.0, retries=3):
    """探出门户节点**真正提供**的 caps（格式 + 缓冲区尺寸），返回 parse_src_caps 的结果。

    为什么非要先探再拼管道：kwin 的监视器/区域流只提供 EnumFormat 里那一种格式
    （实测 `BGRA/BGRx @ 1135x490`，帧率 `0/1` + maxFramerate=60）。客户端要的 caps
    只要不在这个列表里，pipewiresrc 就报 `no more input formats` 并整条管道失败 ✗；
    而下游要的 480x320@10 I420 **一定会反向传到源上**（pipewiresrc 的模板 caps 是 ANY，
    实测源自己被改成下游那套 caps）⇒ 必须先把源钉成它自己的格式，再靠
    videoconvert/videoscale/videorate 去转换。

    探测本身很便宜：`pipewiresrc ! videoconvert ! fakesink`，协商在 PAUSED 阶段就完成
    （实测 caps 打印在 PLAYING 之前），所以不需要等出帧。**要重试**：会话 Start 之后
    第一条连接实测会随机报 `stream error: target not found`（下一条就好）。
    """
    last = None
    for attempt in range(1, retries + 1):
        fd = open_pipewire_remote(bus, session_handle)
        cmd = ["gst-launch-1.0", "-v", "-m", "pipewiresrc", f"fd={fd}", f"path={node}",
               "num-buffers=1", "!", "videoconvert", "!", "fakesink", "sync=false"]
        proc = subprocess.Popen(cmd, pass_fds=(fd,), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        try:
            out = proc.communicate(timeout=timeout_s)[0]
        except subprocess.TimeoutExpired:
            # SIGINT 让 gst-launch 自己收尾（pipeline → NULL 再断开连接），别硬杀。
            # 硬杀是上一轮采集失败的可疑原因之一（**未验证**），而优雅退出成本为零。
            proc.send_signal(signal.SIGINT)
            try:
                out = proc.communicate(timeout=3)[0]
            except subprocess.TimeoutExpired:
                proc.kill()
                out = proc.communicate()[0]
        finally:
            os.close(fd)
        fields = parse_src_caps(out)
        if fields:
            return fields
        last = next((ln.strip() for ln in out.splitlines() if "stream error" in ln), "无 caps")
        if attempt < retries:
            print(f"  探测第 {attempt} 次没拿到 caps（{last}），换一条连接重试", flush=True)
    raise RuntimeError(f"探测门户 caps 失败（{retries} 次）：{last}")


def portal_pipeline_cmd(fd, target, pin, fps, quality, save_dir=None, save_count=6):
    """采集管道：源钉成节点自己的 caps → 转格式/尺寸/帧率 → JPEG → stdout。

    顺序不能变：`pin` 必须在转换器**之前**（钉住源），下游 caps 只能在转换器之后钉，
    否则约束会反向传到源上（实测 `no more input formats` / `set output format: -22` ✗）。

    save_dir 非空时用 tee 分一路把最后 save_count 张 JPEG 落盘：**判据要看帧内容**，
    设备侧的 `0 bad` 只说明解码没报错，不能证明画面是对的（踩过：一片暗灰也 0 bad）。
    """
    cmd = ["gst-launch-1.0", "-q", "pipewiresrc", f"fd={fd}", target,
           "!", pin,
           "!", "videoconvert", "!", "videoscale",
           # drop-only：只丢不复制 —— 静止画面本来不该重复发同一帧（实测 43 帧
           # 全是同一张、挤在 30 ms 里，把 12.75 MB/s 灌进一条 5 MB/s 的链路 ✗）
           "!", "videorate", "drop-only=true",
           # framerate 必须是整数分数（写 30.0/1 会让 caps 非法、pipeline 链接失败 ✗）；
           # 显式给 format=I420，省得 videoscale 与 jpegenc 协商失败
           "!", f"video/x-raw,format=I420,width={W},height={H},framerate={int(fps)}/1",
           "!", "jpegenc", f"quality={quality}"]
    if not save_dir:
        return cmd + ["!", "fdsink", "fd=1", "sync=true"]
    os.makedirs(save_dir, exist_ok=True)
    return cmd + ["!", "tee", "name=t",
                  "t.", "!", "queue", "!", "multifilesink",
                  f"location={os.path.join(save_dir, 'f_%04d.jpg')}",
                  f"max-files={save_count}",
                  "t.", "!", "queue", "!", "fdsink", "fd=1", "sync=true"]


def resolve_portal_pin(bus, session_handle, node):
    """决定要钉在源上的 capsfilter，返回 (pin 字符串, 说明)。

    优先用 pw-dump 里**节点自己声明**的格式（不占连接，实测这组 caps 直接可用 ✓）；
    读不到才退回起一条 gst 探测管道（会多占一条连接）。
    """
    declared = read_node_info(node).get("enum_formats") or []
    pin = pin_from_declared(declared)
    if pin:
        return pin, f"pw-dump 节点声明：{pin}"
    print("pw-dump 读不到节点格式，退回 gst 探测（会多占一条连接）", flush=True)
    fields = discover_portal_caps(bus, session_handle, node)
    pin = caps_pin(fields)
    return pin, f"gst 探测协商出的 caps {fields.get('raw')}"


def spawn_portal_capture(bus, session_handle, target, pin, args):
    """起一条采集管道（**不做存活判断**，由调用方按"有没有出帧"判断）。

    故意不在这里等待：一启动就得开始读 stdout，否则子进程写满管道会阻塞、攒下的帧在
    我们开始读时一次性涌出，把 fps 算成几百（实测 17 帧挤进 0.03 s ✗）。
    """
    fd = open_pipewire_remote(bus, session_handle)
    cmd = portal_pipeline_cmd(fd, target, pin, args.fps, args.jpeg_quality,
                              save_dir=args.save_frames, save_count=args.save_count)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, pass_fds=(fd,))
    os.close(fd)
    return proc


def build_capture(args):
    """返回 (starters, 说明)：每个 starter 是"起一条采集管道"的零参调用。

    portal 路径给出**多个** starter（不同的连接目标写法，按可靠性排序），
    ffmpeg 路径只有一个。调用方按顺序轮着试，死了就换下一种写法重来。
    """
    if args.source.startswith("video="):
        path = args.source.split("=", 1)[1]
        if not os.path.exists(path):
            raise RuntimeError(f"视频文件不存在：{path}")
        # -pix_fmt yuvj420p 是**必须**的：ffmpeg 默认的 MJPEG 采样因子（三个分量都 h=1,v=2）
        # P4 硬件解码器不认（实测 "Sampling factor cannot be recognized" ✗）；
        # 标准 4:2:0 才与已验证可解的 PIL subsampling=2 一致 ✓
        cmd = ["ffmpeg", "-loglevel", "error", "-re", "-stream_loop", "-1", "-i", path,
               "-an", "-vf", f"scale={W}:{H},fps={args.fps}", "-pix_fmt", "yuvj420p",
               "-q:v", str(args.qscale), "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        return [(lambda: subprocess.Popen(cmd, stdout=subprocess.PIPE), "ffmpeg 解码文件")], "video"
    if args.source == "testsrc":
        cmd = ["ffmpeg", "-loglevel", "error", "-re", "-f", "lavfi",
               "-i", f"testsrc=size={W}x{H}:rate={args.fps}", "-pix_fmt", "yuvj420p",
               "-q:v", str(args.qscale), "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        return [(lambda: subprocess.Popen(cmd, stdout=subprocess.PIPE), "ffmpeg 合成测试图")], "testsrc"

    # portal：一次授权；要钉什么 caps 优先从 pw-dump 读（不占连接），再起采集管道
    bus, session_handle, streams = open_screencast_session()
    node = streams[0][0]
    if args.gst_pipeline:
        def start_override():
            fd = open_pipewire_remote(bus, session_handle)
            cmd = (args.gst_pipeline.replace("{fd}", str(fd))
                   .replace("{node}", str(node)).split())
            if cmd[0] != "gst-launch-1.0":        # 允许只写 "pipewiresrc ... ! ..."
                cmd = ["gst-launch-1.0", "-q"] + cmd
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, pass_fds=(fd,))
            os.close(fd)
            return proc
        return [(start_override, "自定义 --gst-pipeline")], "override"

    info = read_node_info(node)
    pin, how = resolve_portal_pin(bus, session_handle, node)
    targets = target_variants(node, info)
    print(f"源 caps（{how}）", flush=True)
    print(f"钉住: {pin}", flush=True)
    print(f"连接目标写法（按可靠性排序）: {targets}", flush=True)
    if args.save_frames:
        print(f"最后 {args.save_count} 张 JPEG 存到 {args.save_frames}/（用来验帧内容）",
              flush=True)
    starters = [(lambda t=t: spawn_portal_capture(bus, session_handle, t, pin, args), t)
                for t in targets]
    return starters, "portal"


def pump(proc, sock, deadline, state):
    """读一帧发一帧；返回这一轮的事实（时间戳是真的：一启动就读 stdout，不先盲等）。

    帧号 state["idx"] 跨轮次累加，所以设备侧看到的帧号始终单调，重启不会造成回绕。
    """
    t_first = t_last = None
    frames = sent = 0
    t_log, win = time.monotonic(), 0
    for jpeg in jpeg_frames(proc.stdout):
        now = time.monotonic()
        if deadline and now >= deadline:
            break
        if sock is not None:
            cnt = (len(jpeg) + UDP_PAYLOAD - 1) // UDP_PAYLOAD
            for i in range(cnt):
                chunk = jpeg[i * UDP_PAYLOAD:(i + 1) * UDP_PAYLOAD]
                sock.send(HDR.pack(state["idx"] & 0xFFFFFFFF, i, cnt, len(jpeg)) + chunk)
            sent += len(jpeg)
        if t_first is None:
            t_first = now
        t_last = now
        state["idx"] += 1
        frames += 1
        win += 1
        if now - t_log >= 1.0:
            print(f"  {win/(now-t_log):5.1f} fps | frame {state['idx']-1} | {len(jpeg)} B",
                  flush=True)
            t_log, win = now, 0
    return {"frames": frames, "sent": sent, "t_first": t_first, "t_last": t_last}


def run_capture(args, sock, starters, deadline):
    """按目标写法轮着起采集管道，直到用满时间预算或轮次用尽；返回 (state, runs)。"""
    state = {"idx": 0}
    runs, interrupted = [], False
    for k in range(args.max_attempts):
        if deadline and time.monotonic() >= deadline:
            break
        start, label = starters[k % len(starters)]
        print(f"采集管道：{label}（第 {k + 1} 轮）", flush=True)
        proc = start()
        before = state["idx"]
        stats = {}
        try:
            stats = pump(proc, sock, deadline, state)
        except KeyboardInterrupt:
            interrupted = True
        finally:
            gst_rc = proc.poll()          # 先判"是不是自己死的"，再收尾
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        stats.update({"label": label, "gst_exit": gst_rc, "died": gst_rc is not None,
                      "frames_this_round": state["idx"] - before})
        runs.append(stats)
        if interrupted or (deadline and time.monotonic() >= deadline):
            break                       # 时间到，正常收尾
        if not stats["died"]:
            break                       # 自己不退也不到点：等下一次循环继续用同一条
        print(f"  管道自己退出了（exit={gst_rc}），换下一种连接写法重来", flush=True)
    return state, runs


def jpeg_frames(stream):
    """从 MJPEG 字节流里切出完整 JPEG（按 SOI/EOI 标记，够用且不依赖容器格式）。"""
    buf = bytearray()
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        buf += chunk
        while True:
            s = buf.find(b"\xff\xd8")
            e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
            if s < 0 or e < 0:
                if len(buf) > 4 * 1024 * 1024:      # 找不到边界就丢，避免无限增长
                    del buf[:-1024]
                break
            yield bytes(buf[s:e + 2])
            del buf[:e + 2]


def main() -> int:
    ap = argparse.ArgumentParser(description="live capture -> JPEG -> P4 display")
    ap.add_argument("host")
    ap.add_argument("--source", default="portal",
                    help="portal | testsrc | video=<file>")
    ap.add_argument("--port", type=int, default=5002)
    ap.add_argument("--fps", type=float, default=30.0, help="capture/encode rate")
    ap.add_argument("--jpeg-quality", type=int, default=75, help="gst jpegenc (portal)")
    ap.add_argument("--qscale", type=int, default=5, help="ffmpeg JPEG qscale (2..31)")
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = until Ctrl-C")
    ap.add_argument("--dry-run", action="store_true",
                    help="只采集+编码，不发送（用来单独量 PC 侧成本）")
    ap.add_argument("--min-fps", type=float, default=30.0, help="PASS 阈值（PC 侧编码帧率）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--probe", action="store_true",
                    help="只做 portal 握手，然后用最简 gst 管道试协商（配合 GST_DEBUG=3 用）")
    ap.add_argument("--gst-pipeline", default="", help="覆盖默认的 gst 管道（含 fd/path 占位）")
    ap.add_argument("--save-frames", default="",
                    help="portal 路径下：额外把最后 N 张 JPEG 落盘（验帧内容用）")
    ap.add_argument("--save-count", type=int, default=6, help="落盘保留的帧数")
    ap.add_argument("--max-attempts", type=int, default=4,
                    help="采集管道自己死掉后最多重起多少轮（每轮换一种连接目标写法）")
    args = ap.parse_args()

    if args.probe:
        try:
            fd, node = open_portal_screencast()
        except RuntimeError as e:
            print(f"ENVIRONMENT_ERROR: {e}", file=sys.stderr)
            return 3
        print(f"portal: pipewire fd={fd} node={node}", flush=True)
        if args.gst_pipeline:
            cmd = args.gst_pipeline.replace("{fd}", str(fd)).replace("{node}", str(node)).split()
            if cmd[0] != "gst-launch-1.0":        # 允许只写 "pipewiresrc ... ! ..."
                cmd = ["gst-launch-1.0", "-v", "-m"] + cmd
        else:
            cmd = ["gst-launch-1.0", "-v", "-m", "pipewiresrc", f"fd={fd}", f"path={node}",
                   "!", "videoconvert", "!", "fakesink", "sync=false"]
        print("probe:", " ".join(cmd), flush=True)
        try:
            # 管道一直跑才算成功；超时被 kill 是预期结果，不是失败
            rc = subprocess.call(cmd, pass_fds=(fd,), timeout=12)
            print(f"probe exit={rc}（非 0 而立刻退出 = 失败）", flush=True)
        except subprocess.TimeoutExpired:
            print("probe: 仍在运行 => 协商成功 ✓", flush=True)
        return 0

    try:
        starters, _note = build_capture(args)
    except RuntimeError as e:
        print(f"ENVIRONMENT_ERROR: {e}", file=sys.stderr)
        return 3

    sock = None
    if not args.dry_run:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect((args.host, args.port))

    t0 = time.monotonic()          # 起点（含 ffmpeg/gst 初始化）
    deadline = t0 + args.seconds if args.seconds > 0 else None
    state, runs = run_capture(args, sock, starters, deadline)

    frames = state["idx"]
    sent = sum(r["sent"] for r in runs)
    firsts = [r["t_first"] for r in runs if r["t_first"]]
    lasts = [r["t_last"] for r in runs if r["t_last"]]
    total = (lasts[-1] - firsts[0]) if (firsts and lasts) else 0.0
    total = max(total, 1e-6)
    fps = frames / total if firsts else 0.0
    died = any(r["died"] for r in runs)
    restarts = max(0, len(runs) - 1)

    # 判据（ORACLE）：
    #   ORACLE: 采集进程全程没自己退出 + 出帧率 >= 阈值
    #   SOURCE: 阈值来自需求（链路 4.95~5.03 MB/s，60 fps 只需 0.66 MB/s）
    #   EXPECTED: 屏幕有变化时 >= min_fps。`drop-only` 下静止画面不出帧是**预期行为**，
    #             此时应把 --min-fps 降到 0，只验"链路活着、管道没死"
    if died:
        last_dead = [r for r in runs if r["died"]][-1]
        verdict = "FAIL"
        why = f"采集管道自己退出了（{last_dead['label']} exit={last_dead['gst_exit']}）"
    elif fps >= args.min_fps:
        verdict, why = "PASS", ""
    else:
        verdict, why = "FAIL", f"出帧率 {fps:.1f} < {args.min_fps}"
    result = {"source": args.source, "frames": frames, "seconds": round(total, 3),
              "fps": round(fps, 2), "mbyte_per_s": round(sent / total / 1e6, 3),
              "min_fps_threshold": args.min_fps, "gst_died": died, "restarts": restarts,
              "rounds": [{k: r[k] for k in ("label", "frames_this_round", "gst_exit", "died")}
                         for r in runs],
              "verdict": verdict}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        # flush：日志可能是被 tee 到管道的（块缓冲），不加就可能丢最后这几行
        print(f"{frames} frames in {total:.2f} s => {fps:.1f} fps "
              f"({sent/total/1e6:.2f} MB/s)  [min {args.min_fps} fps]  {verdict}"
              + (f"  ← {why}" if why else "")
              + (f"  （重启过 {restarts} 次）" if restarts else ""), flush=True)
        for r in runs:
            print(f"  轮次 {r['label']}: 出帧 {r['frames_this_round']}, "
                  f"gst exit {r['gst_exit']}", flush=True)
        if frames and not died and fps < args.min_fps:
            print("提示：`drop-only` 下帧率取决于屏幕变化量，静止画面不出帧；"
                  "只看链路是否活着就加 --min-fps 0", flush=True)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
