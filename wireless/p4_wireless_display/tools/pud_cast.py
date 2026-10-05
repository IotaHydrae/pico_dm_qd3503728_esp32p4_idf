#!/usr/bin/env python3
"""投屏：把桌面实时投到 P4 面板上（Wayland 桌面镜像）。

    tools/pud_cast.py --host 192.168.50.240 --save-host   # 第一次：记住设备
    tools/pud_cast.py                                     # 以后就这样

**会弹一次系统授权框**：选带**你屏幕名字**的那一项（`Share "<显示器>"`，
列表里第一条、最大的那个，容易被当成标题文字）。选 "virtual screen" 会得到一块
新建的空屏幕 —— 投屏看起来成功，但画面永远不是你的桌面。

停止：Ctrl-C。设备侧只在画面有变化时才收到新帧（这是刻意的：静止画面重复发同一张
纯属浪费链路），所以面板静止不动是正常的。参数与踩坑见
`../../notes/wayland-portal-capture.md`。

退出码：0 成功 / 2 用法错 / 3 环境问题（设备不通、缺 PyGObject / gst-launch / pw-dump）
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pudcli                     # noqa: E402
import pudnet                     # noqa: E402


def missing_deps():
    """投屏需要的系统依赖：PyGObject(gi) 与 gst-launch-1.0。

    缺 gi 最常见的原因是先 `source` 了 IDF 的 export.sh（那会把 python 换成 IDF 的
    环境，里面没有 PyGObject）—— 所以提示里直接把解决办法写出来。
    """
    from shutil import which
    missing = []
    try:
        import gi                                   # noqa: F401
    except ImportError:
        missing.append("PyGObject(gi)：用系统 python 跑（别先 source IDF 的 export.sh），"
                       "或 sudo zypper install python3-gobject")
    if not which("gst-launch-1.0"):
        missing.append("gst-launch-1.0（gstreamer 命令行）")
    if not which("pw-dump"):
        missing.append("pw-dump（pipewire-utils；用来读节点声明的格式）")
    return missing


def main(argv=None):
    ap = argparse.ArgumentParser(description="把桌面投到 P4 无线显示器上")
    pudcli.add_common(ap)
    ap.add_argument("--fps", type=float, default=30.0,
                    help="采集侧的帧率上限（实际速率还取决于屏幕变化量：静止画面不出帧）")
    ap.add_argument("--quality", type=int, default=75, help="JPEG 质量 1~100")
    ap.add_argument("--seconds", type=float, default=0.0, help="投多久；0 = 到 Ctrl-C")
    ap.add_argument("--save-frames", default="",
                    help="额外把最后几张 JPEG 存到这个目录（画面不对时用它查证）")
    args = ap.parse_args(argv)

    missing = missing_deps()
    if missing:
        print("投屏缺依赖：\n  - " + "\n  - ".join(missing), file=sys.stderr)
        return pudcli.EXIT_ENVIRONMENT_ERROR

    import portal_capture          # 只有投屏这条路需要 gi，所以放这里才 import

    host = pudcli.host_of(args)
    sender = pudcli.open_sender(args, host)
    if not args.quiet:
        print(f"投屏到 {host}:{args.port}（授权框里选带屏幕名字的那一项；Ctrl-C 停止）")

    def status(msg):
        if not args.quiet:
            print(f"  [采集] {msg}", flush=True)

    recv = {"n": 0}

    def counted(frames):
        """数一下采集侧到底给了多少帧（和送出的帧数对比就知道是谁在限）。"""
        for jpeg in frames:
            recv["n"] += 1
            yield jpeg

    frames = portal_capture.screen_frames(fps=args.fps, quality=args.quality,
                                          save_dir=args.save_frames, on_status=status)
    try:
        # pace="none"：采集侧 `videorate drop-only` 已经按帧率语义限过了，
        # 发送侧再按"到达时刻"丢一次会把一帧里的最新帧丢掉（= 给直播加延迟 ✗）
        return pudcli.run_stream(sender, counted(frames), args, label="投屏",
                                 stop_after_seconds=args.seconds, recv=recv)
    except RuntimeError as exc:                 # 采集侧彻底起不来 = 环境问题
        print(f"采集失败：{exc}", file=sys.stderr)
        return pudcli.EXIT_ENVIRONMENT_ERROR


if __name__ == "__main__":
    sys.exit(main())
