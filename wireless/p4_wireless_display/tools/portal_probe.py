#!/usr/bin/env python3
"""门户采集的**诊断工具**（只产事实，不下结论）：一次授权，跑一组单变量 gst 管道。

为什么需要它：手工一条条试管道时，每次都要用户在桌面上重选一次源，一次只能试一个
管道；而 `set output format: -22` 这类协商问题**必须逐字段定位**（format / 尺寸 / 帧率
各自单独试一遍）。本工具在同一个 portal 会话里反复 `OpenPipeWireRemote` 拿多条新连接，
于是 N 个管道总共只弹**一次**授权框。

工具只产出**事实**：每个管道的退出码、pipewiresrc 实际协商到的 caps、错误文本、
`pw-dump` 给出的节点信息。它不下结论、不判 PASS/FAIL —— 判据在调用者手里
（见工作区 skills/developer-testing）。

用法：
    python3 tools/portal_probe.py                 # 跑全套
    /usr/bin/python3 tools/portal_probe.py --list          # 只列实验项
    /usr/bin/python3 tools/portal_probe.py --only bare,size-480x320
日志与 pw-dump 快照落在 --outdir（默认 /tmp/portal_probe/）。

实验期间请让所选区域里**有东西在动**（视频/滚动的终端）：门户可能只在画面变化时出帧，
静止桌面下"没攒够帧"会被记成超时，而不是失败。

退出码：0 = 跑完（各管道成不成无关）/ 2 = 用法错 / 3 = 环境错误（门户不可用）
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from portal_capture import (parse_src_caps, caps_pin, node_enum_formats,   # noqa: E402
                            open_screencast_session, open_pipewire_remote)

# 每项只钉一个字段，用来分辨"门户到底不接受哪个字段"。
# {src} = pipewiresrc 参数，{sink} = fakesink 参数，{w}/{h} = 门户报表里的区域尺寸。
LADDER = [
    ("bare", "{src} ! fakesink {sink}",
     "不约束任何 caps（基线）：若这条也挂，问题就不在我们的 caps"),
    ("convert", "{src} ! videoconvert ! fakesink {sink}",
     "只加转换器（约定：不该改变协商结果）"),
    ("format-i420", "{src} ! video/x-raw,format=I420 ! fakesink {sink}",
     "只钉 format=I420"),
    ("rate-10", "{src} ! video/x-raw,framerate=10/1 ! fakesink {sink}",
     "只钉帧率 10/1"),
    ("size-480x320", "{src} ! video/x-raw,width=480,height=320 ! fakesink {sink}",
     "只钉尺寸 480x320（≠ 门户给的区域尺寸）"),
    ("size-portal", "{src} ! video/x-raw,width={w},height={h} ! fakesink {sink}",
     "只钉尺寸，用门户报表里的**逻辑**尺寸（≠ 节点的缓冲区尺寸）"),
    ("full-old", "{src} ! videoconvert ! videoscale ! "
                 "video/x-raw,format=I420,width=480,height=320,framerate=10/1 ! "
                 "fakesink {sink}",
     "旧管道：转换器之后钉全套下游 caps"),
]
LADDER_BY_NAME = {name: (tpl, why) for name, tpl, why in LADDER}

ERROR_RE = re.compile(r"ERROR:|stream error|not-negotiated|set output format|"
                      r"no more input formats|could not link|target not found")
BUFFER_LIMIT = 3          # 攒够 3 帧就 EOS：把"能出帧"与"只是协商上了"分开


def build_cmd(tpl, fd, node, width, height):
    """展开模板成 gst-launch 参数表。

    `num-buffers` 交给 pipewiresrc：5 帧后自己 EOS ⇒ 退出码 0 就代表**真的出过帧**，
    不用靠超时去猜。
    """
    src = f"pipewiresrc fd={fd} path={node} num-buffers={BUFFER_LIMIT}"
    pipe = (tpl.replace("{src}", src).replace("{sink}", "sync=false")
               .replace("{w}", str(width)).replace("{h}", str(height)))
    return ["gst-launch-1.0", "-v", "-m"] + pipe.split()


def run_attempt(cmd, fd, timeout, log_path):
    """跑一个管道，返回事实字典（只记录，不解释）。"""
    t0 = time.monotonic()
    timed_out = False
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                pass_fds=(fd,), text=True)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            rc = proc.wait()
    with open(log_path, errors="replace") as f:
        text = f.read()

    fields = parse_src_caps(text)
    errors = []
    for ln in text.splitlines():                 # 错误行去重，保留出现顺序
        if ERROR_RE.search(ln):
            ln = ln.strip()
            if ln not in errors:
                errors.append(ln)
    return {
        "cmd": " ".join(cmd),
        "rc": rc,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - t0, 2),
        "src_caps": fields["raw"] if fields else None,
        "src_caps_fields": fields,
        "errors": errors[:4],
        "log": log_path,
    }


def verdict_of(fact):
    """机械分类，不含成败判断（退出码语义由调用者定）。

    超时要看**有没有协商出 caps**：连不上门户节点时 gst-launch 会一直挂着不出错
    （实测：假 fd 下 7 个管道全部挂到超时且一行 caps 都没有），所以"超时"本身
    不等于"协商成功"。
    """
    if fact["rc"] == 0:
        return f"OK-EOS(出满 {BUFFER_LIMIT} 帧)"
    if fact["timed_out"] and fact["src_caps"]:
        return f"RUNNING(已协商 ✔，但 {fact['seconds']}s 内没满 {BUFFER_LIMIT} 帧)"
    if fact["timed_out"]:
        return f"HANG({fact['seconds']}s 超时且没有任何 caps：没连上/没协商)"
    return f"FAIL(rc={fact['rc']})"


def snapshot_pw_dump(node, outdir, tag):
    """存一份 `pw-dump` 快照，并抽出门户节点的关键事实。

    **辅助证据绝不允许中断扫描**：任何解析问题都只记在返回值里，不抛异常。
    """
    path = os.path.join(outdir, f"pw-dump-{tag}.json")
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=20)
        with open(path, "w") as f:
            f.write(out.stdout)
        objects = json.loads(out.stdout)
    except Exception as e:
        return {"path": path, "error": f"{type(e).__name__}: {e}"}

    result = {"path": path, "found": False}
    for obj in objects:
        if obj.get("id") != node:
            continue
        info = obj.get("info") or {}
        props = info.get("props") or {}
        params = info.get("params")
        result.update({
            "found": True,
            "media_name": props.get("media.name"),
            "node_name": props.get("node.name"),
            "param_types": sorted(params) if isinstance(params, dict) else params,
            "enum_formats": node_enum_formats(params),
        })
    return result


def print_dump(dump):
    """把 pw-dump 抽出的事实打成人读的两三行（Node 自己声明的格式 = 最硬的证据）。"""
    if dump.get("error"):
        print(f"pw-dump: 失败 {dump['error']}", flush=True)
        return
    print(f"pw-dump: {dump.get('path')} found={dump.get('found')} "
          f"params={dump.get('param_types')}", flush=True)
    if dump.get("media_name"):
        print(f"  节点 media.name={dump['media_name']} "
              f"node.name={dump.get('node_name')}", flush=True)
    for e in dump.get("enum_formats") or []:
        print(f"  EnumFormat: {e['width']}x{e['height']} formats={e['formats']} "
              f"dma_buf={e['dma_buf']}", flush=True)


def next_fd(bus, session_handle, prev_fd):
    """每个管道用一条**新**连接（旧连接可能已被上一个 gst 进程关闭）。

    门户若拒绝重复 OpenPipeWireRemote，就退回复用上一条，并把这件事记在结果里 ——
    这是事实，会影响后面结果的解释。
    """
    try:
        return open_pipewire_remote(bus, session_handle), None
    except RuntimeError as e:
        if prev_fd is None:
            raise
        return prev_fd, f"新连接失败（{e}），复用上一条 fd"


def main() -> int:
    ap = argparse.ArgumentParser(description="portal 单变量管道扫描")
    ap.add_argument("--only", default="", help="只跑这些实验项（逗号分隔）")
    ap.add_argument("--list", action="store_true", help="列出实验项后退出")
    ap.add_argument("--timeout", type=float, default=10.0, help="单个管道超时（秒）")
    ap.add_argument("--outdir", default="/tmp/portal_probe", help="日志输出目录")
    ap.add_argument("--json", action="store_true", help="结尾额外打印 JSON 汇总")
    args = ap.parse_args()

    if args.list:
        for name, _tpl, why in LADDER:
            print(f"{name:14s} {why}")
        return 0

    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(LADDER_BY_NAME)
    unknown = [n for n in names if n not in LADDER_BY_NAME]
    if unknown:
        print(f"unknown probes: {unknown}（--list 看可选）", file=sys.stderr)
        return 2

    os.makedirs(args.outdir, exist_ok=True)
    try:
        bus, session_handle, streams = open_screencast_session()
    except RuntimeError as e:
        print(f"ENVIRONMENT_ERROR: {e}", file=sys.stderr)
        return 3

    node = streams[0][0]
    props = streams[0][1]
    size = props.get("size") or ()
    width, height = tuple(size)[:2] if len(tuple(size)) >= 2 else (0, 0)
    print(f"portal node={node} size={width}x{height} "
          f"source_type={props.get('source_type')}", flush=True)

    dump_before = snapshot_pw_dump(node, args.outdir, "before")
    print_dump(dump_before)
    dump_after = {}

    results = {}
    state = {"fd": None}      # 当前持有的 fd：下个实验开新连接，旧的等它用完再关

    def attempt(name, tpl, why):
        """跑一个实验，把事实收进 results 并打一行摘要。"""
        fd, note = next_fd(bus, session_handle, state["fd"])
        fact = run_attempt(build_cmd(tpl, fd, node, width, height), fd, args.timeout,
                           os.path.join(args.outdir, f"{name}.log"))
        fact.update({"why": why, "verdict": verdict_of(fact)})
        if note:
            fact["fd_note"] = note
        results[name] = fact
        print(f"    -> {fact['verdict']}  ({fact['seconds']}s)", flush=True)
        print(f"       src caps: {fact['src_caps']}", flush=True)
        for e in fact["errors"]:
            print(f"       ! {e}", flush=True)
        if note:
            print(f"       ~ {note}", flush=True)
        if state["fd"] is not None and state["fd"] != fd:
            os.close(state["fd"])
        state["fd"] = fd
        return fact

    try:
        for name in names:
            tpl, why = LADDER_BY_NAME[name]
            if "{w}" in tpl and not (width and height):
                results[name] = {"skipped": "门户没给 size，无法构造该实验"}
                print(f"[{name}] SKIP：门户没给 size", flush=True)
                continue
            print(f"[{name}] {why}", flush=True)
            attempt(name, tpl, why)

        # ---- 动态候选：把"探测出来 / 节点声明"的 caps 钉在源上，跑**真实采集管道的尾部** ----
        # 两个自变量：(a) pin 从哪来 —— 先用一次探测连接协商出的（= 真实脚本的做法），
        # 还是直接从 pw-dump 里节点声明的（= 已验证可行的 node-fix 做法）；
        # (b) 尾部形状 —— 只 fakesink，还是接 jpegenc（真实脚本的做法）。
        def pipeline(pin, fps, jpeg, rate):
            parts = ["{src}", "!", pin, "!", "videoconvert", "!", "videoscale"]
            if rate:
                parts += ["!", "videorate"]
            parts += ["!", f"video/x-raw,format=I420,width=480,height=320,"
                           f"framerate={fps}/1"]
            if jpeg:
                parts += ["!", "jpegenc", "quality=75"]
            return " ".join(parts + ["!", "fakesink", "{sink}"])

        base = next((n for n in ("bare", "convert")
                     if results.get(n, {}).get("src_caps_fields")), None)
        pins = []
        if base:
            pins.append(("discovered", caps_pin(results[base]["src_caps_fields"]),
                         f"{base} 协商出的 caps"))
        node_fmts = [e for e in (dump_before.get("enum_formats") or [])
                     if e.get("width") and e.get("height") and e.get("formats")]
        if node_fmts:
            e = next((x for x in node_fmts if not x["dma_buf"]), node_fmts[0])
            pins.append(("declared",
                         f"video/x-raw,format={e['formats'][0]},"
                         f"width={e['width']},height={e['height']}",
                         "pw-dump 里节点声明的 caps"))

        for pin_name, pin, pin_why in pins:
            for fps, jpeg, rate in ((10, False, True), (30, True, True), (10, True, True)):
                name = f"{pin_name}-{fps}-{'jpeg' if jpeg else 'fakesink'}"
                tpl = pipeline(pin, fps, jpeg, rate)
                why = f"{pin_why}（{pin}）+ 尾部 {fps}fps/{'jpeg' if jpeg else 'raw'}"
                print(f"[{name}] {why}", flush=True)
                attempt(name, tpl, why)
                results[name]["pinned_caps"] = pin
        if not pins:
            print("[*] 跳过全部候选：既没探测到 caps，pw-dump 里也没有 EnumFormat", flush=True)

        # 收尾再抓一次 pw-dump：**协商成功**之后节点上的 Format 会从 [] 变成实际格式，
        # 这是"我们到底让它设了什么"的直接证据。
        dump_after = snapshot_pw_dump(node, args.outdir, "after")
    except RuntimeError as e:
        print(f"ENVIRONMENT_ERROR: 取不到 PipeWire fd：{e}", file=sys.stderr)
        if state["fd"] is not None:
            os.close(state["fd"])
        return 3

    if state["fd"] is not None:
        os.close(state["fd"])

    print("\n=== 汇总（事实，不含结论）===")
    for name, r in results.items():
        print(f"{name:22s} {r.get('verdict', r.get('skipped', '?'))}")
    print_dump(dump_after)
    if args.json:
        print(json.dumps({"node": node, "size": [width, height],
                          "source_type": props.get("source_type"),
                          "pw_dump_before": dump_before, "pw_dump_after": dump_after,
                          "attempts": results},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
