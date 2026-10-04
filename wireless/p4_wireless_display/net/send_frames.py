#!/usr/bin/env python3
"""PC 侧发帧器：把动画/图片编码成 JPEG，按 [u32 长度][JPEG] 推给 P4 无线显示器。

    # ORACLE: REQUIREMENT
    # SOURCE: 链路实测（p4_wifi_probe/FINDINGS.md）：从机 3.0.9 连测 3 次 4.95~5.03 MB/s；
    #         从机 2.12.13 兼容模式单次 60 s 5.44 MB/s —— 取保守值 ~5 MB/s
    # EXPECTED: 稳定 >= 30 fps 且带宽 < 2 MB/s（留足余量，真机画面不撕裂/不卡顿）

退出码（与工作区 testing skill 一致）：
    0 PASS / 1 FAIL / 2 INVALID_USAGE / 3 ENVIRONMENT_ERROR / 4 TIMEOUT

画面内容刻意做成"一眼能判断"的：移动色条 + 大号帧号 + 秒表。
帧号在屏幕上是连续的 ⇒ 丢帧/花屏立刻看得出来；秒表能对着表测端到端延迟。
"""

import argparse
import io
import json
import socket
import struct
import sys
import time

from PIL import Image, ImageDraw

W, H = 480, 320


def make_frame(idx: int, t_sec: float) -> Image.Image:
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)

    # 移动的竖向色条（1 秒一个来回）：动起来一眼可见，静止=卡住
    shift = int((t_sec % 2.0) / 2.0 * W)
    for i in range(0, W, 64):
        x = (i + shift) % W
        c = ((i // 64) % 3)
        col = [(255, 40, 40), (40, 200, 40), (60, 80, 255)][c]
        d.rectangle([x, 0, min(x + 31, W - 1), H - 120], fill=col)
        if x + 32 > W:  # 环绕的那一小段
            d.rectangle([0, 0, x + 32 - W, H - 120], fill=col)

    # 帧号与秒表（默认点阵字体，放大成大号）
    d.rectangle([0, H - 120, W - 1, H - 1], fill=(0, 0, 0))
    small = Image.new("RGB", (W, 60), (0, 0, 0))
    ImageDraw.Draw(small).text((4, 8), f"frame {idx:6d}", fill=(255, 255, 255))
    img.paste(small.resize((W, 60 * 40 // 60)), (0, H - 118))
    small2 = Image.new("RGB", (W, 60), (0, 0, 0))
    ImageDraw.Draw(small2).text((4, 8), f"t = {t_sec:8.2f} s", fill=(255, 255, 0))
    img.paste(small2, (0, H - 56))
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description="push JPEG frames to the P4 display")
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=None,
                    help="default 5002 for UDP, 5001 for TCP")
    ap.add_argument("--udp", action="store_true",
                    help="send each frame as UDP fragments (12 B header + 1400 B payload); "
                         "the device drops an incomplete frame as soon as a newer one starts")
    ap.add_argument("--fps", type=float, default=60.0, help="target frames/s")
    ap.add_argument("--quality", type=int, default=60)
    ap.add_argument("--seconds", type=float, default=20.0, help="0 = forever")
    ap.add_argument("--min-fps", type=float, default=30.0, help="PASS threshold")
    ap.add_argument("--preencode", type=int, default=0,
                    help="pre-encode N frames once, then loop them (measures the "
                         "link+pipeline ceiling instead of PIL's encoding speed)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.udp and args.fps <= 0:
        # UDP 的 send() 不阻塞：不限速时 PC 会以远超链路的速度"发"，绝大多数包在本地
        # 就丢了，PC 侧的 fps 变成"往内核塞包的速度" —— 这种数字没有意义 ✗
        print("INVALID_USAGE: --udp 必须配合 --fps N（UDP 不限速时 PC 侧计数无意义，"
              "请用设备侧的 shown/dropped 判据）", file=sys.stderr)
        return 2
    port = args.port if args.port else (5002 if args.udp else 5001)
    if args.udp:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect((args.host, port))          # 便于本地回 ICMP 错误
        udp_payload, udp_hdr = 1400, struct.Struct("<IHHI")
    else:
        try:
            sock = socket.create_connection((args.host, port), timeout=5)
        except OSError as e:
            print(f"ENVIRONMENT_ERROR: {args.host}:{port}: {e}", file=sys.stderr)
            return 3

    period = 1.0 / args.fps if args.fps > 0 else 0.0
    preroll = []
    if args.preencode > 0:
        for k in range(args.preencode):
            b = io.BytesIO()
            make_frame(k, k / 60.0).save(b, "JPEG", quality=args.quality,
                                         subsampling=2, optimize=False)
            preroll.append(b.getvalue())
        print(f"pre-encoded {len(preroll)} frames "
              f"({sum(len(p) for p in preroll)//len(preroll)} B avg)", flush=True)
    t0 = time.monotonic()
    deadline = t0 + args.seconds if args.seconds > 0 else None
    idx = 0
    sent_bytes = 0
    t_log = t0
    win_frames = 0
    win_bytes = 0
    min_window_fps = None

    try:
        while deadline is None or time.monotonic() < deadline:
            now = time.monotonic()
            t_sec = now - t0
            if preroll:
                payload = preroll[idx % len(preroll)]
            else:
                buf = io.BytesIO()
                make_frame(idx, t_sec).save(buf, "JPEG", quality=args.quality,
                                            subsampling=2, optimize=False)
                payload = buf.getvalue()
            if args.udp:
                cnt = (len(payload) + udp_payload - 1) // udp_payload
                for i in range(cnt):
                    chunk = payload[i * udp_payload:(i + 1) * udp_payload]
                    sock.send(udp_hdr.pack(idx & 0xFFFFFFFF, i, cnt, len(payload)) + chunk)
            else:
                sock.sendall(struct.pack("<I", len(payload)) + payload)
            sent_bytes += len(payload)
            idx += 1
            win_frames += 1
            win_bytes += len(payload)

            if now - t_log >= 1.0:
                fps = win_frames / (now - t_log)
                min_window_fps = fps if min_window_fps is None else min(min_window_fps, fps)
                print(f"  {fps:5.1f} fps | {win_bytes/1e6:5.2f} MB/s | "
                      f"frame {idx-1} | {len(payload)} B/frame", flush=True)
                t_log, win_frames, win_bytes = now, 0, 0

            if period:
                sleep = period - (time.monotonic() - now)
                if sleep > 0:
                    time.sleep(sleep)
    except (socket.timeout, OSError) as e:
        print(f"ENVIRONMENT_ERROR: send failed: {e}", file=sys.stderr)
        return 3

    total = time.monotonic() - t0
    fps = idx / total if total > 0 else 0.0
    verdict = "PASS" if fps >= args.min_fps else "FAIL"
    if args.json:
        print(json.dumps({"host": args.host, "transport": "udp" if args.udp else "tcp",
                          "frames": idx, "seconds": round(total, 3),
                          "fps": round(fps, 2), "min_window_fps": round(min_window_fps or 0, 2),
                          "mbyte_per_s": round(sent_bytes / total / 1e6, 3),
                          "min_fps_threshold": args.min_fps, "verdict": verdict}))
    else:
        print(f"{idx} frames in {total:.2f} s => {fps:.1f} fps "
              f"({sent_bytes/total/1e6:.2f} MB/s), worst 1 s window "
              f"{min_window_fps:.1f} fps  [min {args.min_fps} fps]  {verdict}")
    sock.close()
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
