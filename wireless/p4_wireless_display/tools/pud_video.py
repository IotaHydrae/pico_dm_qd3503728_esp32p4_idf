#!/usr/bin/env python3
"""放视频：把视频文件（或一个目录里的视频）播到 P4 面板上。

    tools/pud_video.py --host 192.168.50.240 clip.mp4      # 循环播放到 Ctrl-C
    tools/pud_video.py clip.mp4 --once                     # 播一遍就退出
    tools/pud_video.py ~/Videos/                           # 目录：逐个播，播完再从第一个开始

画面按真实速度播（ffmpeg `-re`），缩放到 480x320 **保持比例加黑边**（不拉扁）。
目录播放时每个文件只播一遍（否则列表永远走不到下一个），整体循环由 `--once` 控制。

退出码：0 成功 / 2 路径或用法错 / 3 环境问题（设备不通、ffmpeg 缺失）
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pudcli                     # noqa: E402
import pud_media                  # noqa: E402
from pudnet import jpegs_from     # noqa: E402


def play_one(sender, path, args, loop):
    """播一个文件到发送完毕（或 Ctrl-C）。返回 pudcli 退出码。"""
    try:
        proc = pud_media.video_stream(path, fps=args.fps, quality=args.quality,
                                      loop=loop)
    except FileNotFoundError:
        print(f"文件不存在：{path}", file=sys.stderr)
        return pudcli.EXIT_INVALID_USAGE
    try:
        # 放视频用 real：按 1/fps 等时间点 ⇒ 3 秒的片子真的播 3 秒
        rc = pudcli.run_stream(sender, jpegs_from(proc.stdout), args,
                               label=os.path.basename(path),
                               stop_after_seconds=args.seconds,
                               fps=args.fps, pace="real")
        if rc != pudcli.EXIT_OK:        # 只在真失败时回显 ffmpeg 的 stderr
            err = pud_media.ffmpeg_stderr(proc)
            if err:
                print(f"ffmpeg: {err}", file=sys.stderr)
        return rc
    finally:
        proc.terminate()


def play_list(sender, files, args):
    """目录播放：文件逐个播一遍；`--once` 之外整体循环，直到 Ctrl-C。"""
    while True:
        for path in files:
            if sender.interrupted:
                return pudcli.EXIT_OK
            rc = play_one(sender, path, args, loop=False)
            if rc != pudcli.EXIT_OK:
                return rc
        if args.once:
            return pudcli.EXIT_OK


def video_files(directory):
    return [f for f in pud_media.list_media(directory)
            if pud_media.kind_of(f) == "video"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="把视频播到 P4 无线显示器上")
    ap.add_argument("path", help="视频文件，或装着视频的目录")
    pudcli.add_common(ap)
    ap.add_argument("--fps", type=float, default=30.0, help="投出去的帧率（上限 30）")
    ap.add_argument("--quality", type=int, default=85, help="JPEG 质量 1~100（越大越好；默认 85 ⇒ ffmpeg -q:v 6）")
    ap.add_argument("--once", action="store_true", help="播一遍就退出（默认循环）")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="最多播几秒（0 = 不限；目录播放时对每个文件各自计时）")
    args = ap.parse_args(argv)

    if not os.path.exists(args.path):
        print(f"路径不存在：{args.path}", file=sys.stderr)
        return pudcli.EXIT_INVALID_USAGE

    host = pudcli.host_of(args)
    sender = pudcli.open_sender(args, host)
    try:
        if os.path.isdir(args.path):
            files = video_files(args.path)
            if not files:
                print(f"目录里没有可播的视频：{args.path}", file=sys.stderr)
                return pudcli.EXIT_INVALID_USAGE
            if not args.quiet:
                print(f"播放 {len(files)} 个文件到 {host}（Ctrl-C 停止）")
            return play_list(sender, files, args)
        if not args.quiet:
            print(f"播放 {os.path.basename(args.path)} 到 {host}（Ctrl-C 停止）")
        return play_one(sender, args.path, args, loop=not args.once)
    except KeyboardInterrupt:
        return pudcli.EXIT_OK
    finally:
        sender.close()


if __name__ == "__main__":
    sys.exit(main())
