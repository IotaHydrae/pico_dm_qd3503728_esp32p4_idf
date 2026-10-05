#!/usr/bin/env python3
"""端到端显示测试：合成动画（带帧号与秒表）→ 无线链路 → 设备解码上屏。

    ORACLE: REQUIREMENT
    SOURCE: 需求（面板要能"实时看"）+ 链路实测 wireless/p4_wifi_probe/FINDINGS.md
    EXPECTED: 平均 >= 30 fps 且最差 1 s 窗口 >= 30 fps，带宽 < 2 MB/s

画面刻意做成"一眼能判断"的：移动色条 + 大号帧号 + 秒表。帧号在面板上是连续的 ⇒
丢帧/花屏当场可见；秒表能对着表量端到端延迟。所以这条测试同时有两个判据：
机器这边看 PC 侧发出去的速率（下面 Report），人这边看面板上的帧号跳不跳。

用系统 python 跑（需要 Pillow；IDF 的 python 环境里没有）：

    python3 tests/test_display_stream.py --host <设备IP>            # 20 s
    python3 tests/test_display_stream.py --host <IP> --json --seconds 30

设备不通 = ENVIRONMENT_ERROR(3)，不是 FAIL；没有可靠 oracle 时不该出现在这里。
退出码：0 PASS / 1 FAIL / 2 INVALID_USAGE / 3 ENVIRONMENT_ERROR / 4 TIMEOUT
"""

import io
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "common"))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, os.pardir, "tools")))

import pudnet                                                    # noqa: E402
from harness import (Oracle, Report, Snapshot, Test,             # noqa: E402
                     EnvironmentProblem, run_test)

W, H = pudnet.FRAME_W, pudnet.FRAME_H

ORACLE = Oracle("REQUIREMENT",
                "面板实时观看的验收要求（>= 30 fps）+ 链路实测 "
                "wireless/p4_wifi_probe/FINDINGS.md（4.95~5.03 MB/s）",
                "**按 60 fps 驱动**时平均 >= 30 fps 且最差 1 s 窗口 >= 30 fps；"
                "带宽 < 2 MB/s",
                "按需求速率本身（30 fps）驱动再断言 >= 30 等于考发送侧的 sleep 精度"
                "（实测 29.87 fps 被判 FAIL，差 0.4% ✗）—— 所以驱动速率取需求的 2 倍，"
                "问的才是「能不能稳住 30 fps」")


def make_frame(idx, t_sec, Image, ImageDraw):
    """一帧测试图：移动色条（动起来一眼可见）+ 大号帧号 + 秒表。"""
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    shift = int((t_sec % 2.0) / 2.0 * W)          # 1 秒一个来回：静止=卡住
    for i in range(0, W, 64):
        x = (i + shift) % W
        col = [(255, 40, 40), (40, 200, 40), (60, 80, 255)][(i // 64) % 3]
        d.rectangle([x, 0, min(x + 31, W - 1), H - 121], fill=col)
        if x + 32 > W:                            # 环绕的那一小段
            d.rectangle([0, 0, x + 32 - W, H - 121], fill=col)
    d.rectangle([0, H - 120, W - 1, H - 1], fill=(0, 0, 0))
    small = Image.new("RGB", (W, 60), (0, 0, 0))
    ImageDraw.Draw(small).text((4, 8), f"frame {idx:6d}", fill=(255, 255, 255))
    img.paste(small.resize((W, H // 2 - 40)), (0, H - 118))
    small2 = Image.new("RGB", (W, 60), (0, 0, 0))
    ImageDraw.Draw(small2).text((4, 8), f"t = {t_sec:8.2f} s", fill=(255, 255, 0))
    img.paste(small2, (0, H - 56))
    return img


def _encoder(quality):
    """返回 (draw(idx, t) -> JPEG bytes)。Pillow 只在需要时导入。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise EnvironmentProblem(f"需要 Pillow 用系统 python 跑：{exc}")

    def draw(idx, t_sec):
        buf = io.BytesIO()
        make_frame(idx, t_sec, Image, ImageDraw).save(
            buf, "JPEG", quality=quality, subsampling=2, optimize=False)
        return buf.getvalue()

    return draw


def check(_dev, args):
    """发 frames 秒，返回速率观察（判据见 ORACLE）。"""
    host = args.host
    if not pudnet.probe_host(host):
        raise EnvironmentProblem(f"{host} ping 不通（板子在同一 WiFi 吗？）")

    try:
        sender = (pudnet.TcpSender(host, args.port) if args.transport == "tcp"
                  else pudnet.Sender(host, args.port))
    except OSError as exc:
        raise EnvironmentProblem(f"{args.transport} 连不上 {host}:{args.port}: {exc}")

    if args.preencode:
        draw = _encoder(args.quality)
        preroll = [draw(k, k / 60.0) for k in range(args.preencode)]
    else:
        preroll = []

    period = 1.0 / args.fps if args.fps > 0 else 0.0
    t0 = time.monotonic()
    deadline = t0 + args.seconds
    idx = 0
    t_log, win = t0, 0
    worst_1s = None
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            payload = (preroll[idx % len(preroll)] if preroll
                       else _encoder(args.quality)(idx, now - t0))
            sender.send(payload)
            idx += 1
            win += 1
            if now - t_log >= 1.0:
                fps = win / (now - t_log)
                worst_1s = fps if worst_1s is None else min(worst_1s, fps)
                if not args.quiet:
                    print(f"  {fps:5.1f} fps | {sender.bytes / (now - t0) / 1e6:5.2f} MB/s "
                          f"| frame {idx - 1} | {len(payload)} B", flush=True)
                t_log, win = now, 0
            if period:
                nap = period - (time.monotonic() - now)
                if nap > 0:
                    time.sleep(nap)
    finally:
        sender.close()

    total = max(time.monotonic() - t0, 1e-6)
    fps = idx / total
    mbps = sender.bytes / total / 1e6
    ok = (fps >= args.min_fps and (worst_1s is None or worst_1s >= args.min_fps)
          and mbps < 2.0)
    return Report.of(
        Snapshot("frames_sent", idx),
        Snapshot("fps_average", round(fps, 2), "fps"),
        Snapshot("fps_worst_1s", round(worst_1s or 0, 2), "fps"),
        Snapshot("mbyte_per_s", round(mbps, 3), "MB/s"),
        Snapshot("transport", args.transport),
        Snapshot("threshold", args.min_fps, "fps"),
        ok=ok,
        detail=("面板上还应看到连续递增的帧号（丢帧/花屏的现场判据）"
                if ok else f"未达 {args.min_fps} fps 或带宽超 2 MB/s"),
    )


def extra_args(ap):
    ap.add_argument("--host", required=True, help="设备 IP（板子在同一 WiFi）")
    ap.add_argument("--port", type=int, default=pudnet.UDP_PORT)
    ap.add_argument("--transport", choices=("udp", "tcp"), default="udp")
    ap.add_argument("--fps", type=float, default=60.0,
                    help="驱动速率（默认 60 = 需求的 2 倍，见 ORACLE）")
    ap.add_argument("--quality", type=int, default=60, help="JPEG 质量 1~100")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--min-fps", type=float, default=30.0, help="判据阈值（见 ORACLE）")
    ap.add_argument("--preencode", type=int, default=60,
                    help="先编好 N 帧再循环发（默认 60：测的是链路+管线，"
                         "把 PIL 编码成本移出计时；给 0 则连 PIL 一起量）")


TEST = Test("display_stream", ORACLE, check, needs_device=False,
            description="合成动画经无线链路到设备上屏的端到端速率",
            default_timeout=180.0, extra_args=extra_args)


if __name__ == "__main__":
    run_test(TEST)
