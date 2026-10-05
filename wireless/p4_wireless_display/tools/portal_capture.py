#!/usr/bin/env python3
"""门户采集库：xdg-desktop-portal ScreenCast → PipeWire → pipewiresrc → JPEG。

被 `pud_cast.py`（用户脚本）、`tools/portal_probe.py`（诊断工具）共用。
**踩坑与结论都在 `../../notes/wayland-portal-capture.md`**，这里只放能跑的代码。

三条硬规则（不遵守就采不到真桌面，三条都是实测）：

1. 授权框必须选**真显示器**那一项（`Share "<monitor>"`，列表第一条、最大的那个）；
   选 `virtual screen` 会得到一块新建的空屏，投屏照样"成功"但画面永远不是桌面。
2. **先把源钉成"节点声明过的 caps"再转换**：`pipewiresrc` 的 src 模板 caps 是 `ANY`，
   下游要什么它就把源 fixate 成什么 ⇒ 门户节点是固定尺寸/格式的，直接要 I420 480x320
   会 `no more input formats` / `set output format: -22` 整条死。尺寸钉死、格式取
   声明并集、**帧率不钉**（节点是 0/1）。
3. 连接标识优先 `target-object=<object.serial>`：`path=<数字 id>` 实测会在流跑到一半
   重新解析失败（`target not found`）。
"""

import json
import os
import re
import signal
import subprocess
import time

from pudnet import FRAME_W, FRAME_H, jpegs_from

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
    多个候选管道共用一次用户授权（见 tools/portal_probe.py）。
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
           # videorate **放在转换器之前**：丢掉的帧就不必再做一次 1080p 的
           # 色彩转换与缩放。实测同一条链的上限 131 fps → 214 fps（1920x1080 BGRA 输入）。
           # drop-only：只丢不复制 —— 静止画面不该重复发同一帧。
           "!", "videorate", "drop-only=true",
           "!", "videoconvert",
           # method=lanczos：默认是 bilinear（2-tap），对 4 倍下采样太糙 —— 实测文字区
           # PSNR 16.37 → 18.39 dB、全图 21.93 → 23.17 dB，代价只有 CPU 慢 14% ✓
           "!", "videoscale", "method=lanczos", "add-borders=true",
           # framerate 必须是整数分数（写 30.0/1 会让 caps 非法、pipeline 链接失败 ✗）；
           # 显式给 format=I420，省得 videoscale 与 jpegenc 协商失败；
           # **pixel-aspect-ratio=1/1 必须写**：不写的话 videoscale 会去改 PAR（显示端就是
           # 拉伸）而不是加黑边 —— 实测 16:9 被拉成 3:2，PSNR 只有 8.55 dB ✗（写上是 21.93）
           "!", f"video/x-raw,format=I420,width={FRAME_W},height={FRAME_H},"
                f"framerate={int(fps)}/1,pixel-aspect-ratio=1/1",
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


def spawn_portal_capture(bus, session_handle, target, pin, fps=30.0, quality=75,
                         save_dir="", save_count=6):
    """起一条采集管道（**不做存活判断**，由调用方按"有没有出帧"判断）。

    故意不在这里等待：一启动就得开始读 stdout，否则子进程写满管道会阻塞、攒下的帧在
    我们开始读时一次性涌出，把 fps 算成几百（实测 17 帧挤进 0.03 s ✗）。
    """
    fd = open_pipewire_remote(bus, session_handle)
    cmd = portal_pipeline_cmd(fd, target, pin, fps, quality,
                              save_dir=save_dir, save_count=save_count)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, pass_fds=(fd,))
    os.close(fd)
    return proc

def screen_frames(fps=30.0, quality=75, save_dir="", save_count=6, max_attempts=4,
                  on_status=None):
    """生成器：产出门户采到的 JPEG 帧（授权、caps 发现、重启监督都在里面）。

    调用方只需要 `for jpeg in screen_frames(...)`：授权一次、caps 自动发现、
    管道自己死掉就换连接写法重起。`on_status` 收到人类可读的状态行（给 CLI 打印用）。
    """
    def status(msg):
        if on_status:
            on_status(msg)

    bus, session_handle, streams = open_screencast_session()
    node = streams[0][0]
    status(f"门户流: {streams[0][1]}")
    info = read_node_info(node)
    pin, how = resolve_portal_pin(bus, session_handle, node)
    status(f"源 caps（{how}）")
    status(f"钉住: {pin}")
    targets = target_variants(node, info)
    status(f"连接目标: {targets}")

    for attempt in range(max_attempts):
        target = targets[attempt % len(targets)]
        status(f"采集管道: {target}（第 {attempt + 1} 轮）")
        proc = spawn_portal_capture(bus, session_handle, target, pin, fps, quality,
                                    save_dir, save_count)
        gst_rc = None
        try:
            for jpeg in jpegs_from(proc.stdout):
                yield jpeg
        finally:
            gst_rc = proc.poll()          # 先判"是不是自己死的"，再收尾
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        if gst_rc is None:
            return                        # 调用方主动停的：正常结束
        status(f"  管道自己退出了（exit={gst_rc}），换连接写法重来")
    raise RuntimeError(f"采集管道 {max_attempts} 轮都没起来：看上面 gst 的报错")
