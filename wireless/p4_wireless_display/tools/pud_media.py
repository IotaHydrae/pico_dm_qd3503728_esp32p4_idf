#!/usr/bin/env python3
"""媒体源库：用 ffmpeg 把**视频 / 图片**变成设备能吃的 JPEG 流（480x320）。

为什么走 ffmpeg：缩放、加黑边、JPEG 编码它都做得很稳，而且**必须**加
`-pix_fmt yuvj420p` —— P4 硬件解码器不认 ffmpeg 默认的 MJPEG 采样布局
（三个分量都 h=1,v=2；实测报 `Sampling factor cannot be recognized` ✗），
标准 4:2:0 才行。这条在 `../../notes/jpeg-hardware-decode.md` 有记录。

画面**一律缩放到 480x320 并保持比例加黑边**：面板是固定的 480x320（旋转 1），
让 16:9 的视频被拉扁不是我们想要的默认行为。
"""

import os
import subprocess

from pudnet import FRAME_W, FRAME_H

VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts", ".flv", ".gif")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")

# 保持比例、居中、补黑边到 480x320
_FIT = (f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease,"
        f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2")


def _ffmpeg(args):
    return subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error"] + args,
                            stdout=subprocess.PIPE)


def video_stream(path, fps=30.0, quality=60, loop=True):
    """视频 → JPEG 流（**尽快**解出来，限速由发送侧做）。返回带 stdout 的进程。

    故意**不用** `-re`：实测 3.0 s 的片子加 `-re` 只要 2.46 s 就吐完（偏快 18%），
    于是播放速率变成"ffmpeg 说了算"、我们无法解释。放在发送侧限速（`pudcli.run_stream`
    的 `pace="real"`）后，"播 3 秒的片子就花 3 秒"是可以验的。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    args = []
    if loop:
        args += ["-stream_loop", "-1"]
    args += ["-i", path, "-an", "-vf", f"{_FIT},fps={fps}", "-pix_fmt", "yuvj420p",
             "-q:v", str(quality), "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    return _ffmpeg(args)


def image_jpeg(path, quality=60):
    """图片 → 一张 480x320 的 JPEG（字节）。"""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    proc = _ffmpeg(["-i", path, "-frames:v", "1", "-vf", _FIT, "-pix_fmt", "yuvj420p",
                    "-q:v", str(quality), "-f", "image2pipe", "-vcodec", "mjpeg", "-"])
    data, _ = proc.communicate()
    if not data:
        raise RuntimeError(f"ffmpeg 没解出图：{path}")
    return data


def testsrc_stream(fps=30.0, quality=60):
    """合成测试图（永远可用，不需要任何输入文件）—— 测试与自检用。"""
    return _ffmpeg(["-f", "lavfi", "-i", f"testsrc=size={FRAME_W}x{FRAME_H}:"
                                       f"rate={fps}", "-pix_fmt", "yuvj420p",
                    "-q:v", str(quality), "-f", "image2pipe", "-vcodec", "mjpeg", "-"])


def kind_of(path):
    """粗判一个路径是视频还是图片（按扩展名，够用且不引入探测依赖）。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return None


def list_media(directory):
    """目录里可播的媒体（图片优先按名字排序，用于轮播）。"""
    out = []
    for name in sorted(os.listdir(directory)):
        full = os.path.join(directory, name)
        if os.path.isfile(full) and kind_of(full):
            out.append(full)
    return out
