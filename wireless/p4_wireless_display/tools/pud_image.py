#!/usr/bin/env python3
"""看图：把图片显示到 P4 面板上（单张，或一个目录轮播）。

    tools/pud_image.py --host 192.168.50.240 photo.jpg     # 显示一张就退出
    tools/pud_image.py ~/Pictures/ --seconds 10            # 轮播，每张停 10 秒
    tools/pud_image.py ~/Pictures/ --once                  # 只轮一遍

图片缩放到 480x320 **保持比例加黑边**。同一张会**连发几次**（默认 5 次）：UDP 丢一个
分片就丢一帧，重发几次是最省事的容错；设备对同一画面重复解码上屏没有副作用。

退出码：0 成功 / 2 路径或用法错 / 3 环境问题（设备不通、ffmpeg 缺失或解不开图）
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pudcli                     # noqa: E402
import pud_media                  # noqa: E402

COPIES = 5          # 同一张图连发几次（容错），见文件头说明
COPY_GAP = 0.05     # 两次之间隔多久（秒）


def show(sender, path, args):
    """显示一张图（连发 COPIES 次）。返回 pudcli 退出码。"""
    try:
        jpeg = pud_media.image_jpeg(path, quality=args.quality)
    except FileNotFoundError:
        print(f"文件不存在：{path}", file=sys.stderr)
        return pudcli.EXIT_INVALID_USAGE
    except RuntimeError as exc:
        print(f"解不开这张图：{exc}", file=sys.stderr)
        return pudcli.EXIT_ENVIRONMENT_ERROR
    name = os.path.basename(path)
    for _ in range(COPIES):
        sender.send(jpeg)
        time.sleep(COPY_GAP)
    if not args.quiet:
        print(f"  {name}: 已发送（{len(jpeg)} B × {COPIES}）", flush=True)
    return pudcli.EXIT_OK


def slideshow(sender, files, args):
    """目录轮播：每张停 `--seconds` 秒；`--once` 之外一直轮，直到 Ctrl-C。"""
    while True:
        for path in files:
            if sender.interrupted:
                return pudcli.EXIT_OK
            rc = show(sender, path, args)
            if rc != pudcli.EXIT_OK:
                return rc
            time.sleep(max(args.seconds - COPIES * COPY_GAP, 0.1))
        if args.once:
            return pudcli.EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(description="把图片显示到 P4 无线显示器上")
    ap.add_argument("path", help="图片文件，或装着图片的目录")
    pudcli.add_common(ap)
    ap.add_argument("--quality", type=int, default=85, help="JPEG 质量 1~100（越大越好；默认 85 ⇒ ffmpeg -q:v 6）")
    ap.add_argument("--seconds", type=float, default=10.0, help="轮播时每张停几秒")
    ap.add_argument("--once", action="store_true", help="目录只轮一遍（默认一直轮）")
    args = ap.parse_args(argv)

    if not os.path.exists(args.path):
        print(f"路径不存在：{args.path}", file=sys.stderr)
        return pudcli.EXIT_INVALID_USAGE

    host = pudcli.host_of(args)
    sender = pudcli.open_sender(args, host)
    try:
        if os.path.isdir(args.path):
            files = [f for f in pud_media.list_media(args.path)
                     if pud_media.kind_of(f) == "image"]
            if not files:
                print(f"目录里没有可显示的图片：{args.path}", file=sys.stderr)
                return pudcli.EXIT_INVALID_USAGE
            if not args.quiet:
                print(f"轮播 {len(files)} 张到 {host}，每张 {args.seconds:g} s"
                      f"（Ctrl-C 停止）")
            return slideshow(sender, files, args)
        if not args.quiet:
            print(f"显示 {os.path.basename(args.path)} 到 {host}")
        return show(sender, args.path, args)
    except KeyboardInterrupt:
        return pudcli.EXIT_OK
    finally:
        sender.close()


if __name__ == "__main__":
    sys.exit(main())
