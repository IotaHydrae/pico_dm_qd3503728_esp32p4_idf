#!/usr/bin/env python3
"""主机侧吞吐打流器：把字节流推到设备的 TCP sink，报告持续吞吐。

    # ORACLE: REQUIREMENT
    # SOURCE: 480x320 JPEG q60 @60fps = 11048 B * 60 = 0.66 MB/s (5.3 Mbps)
    #         这是"无线显示器够用"的最低要求，留 50% 余量 => 阈值 1 MB/s
    # EXPECTED: >= 1.0 MB/s

退出码（与工作区 testing skill 一致）：
    0 PASS / 1 FAIL（低于阈值）/ 2 INVALID_USAGE / 3 ENVIRONMENT_ERROR（连不上）/ 4 TIMEOUT

只产出事实（字节数与耗时），判定规则显式写在上面的 ORACLE 里，阈值可用
--threshold-mbps 覆盖，覆盖时会在输出里标注。
"""

import argparse
import json
import socket
import sys
import time

CHUNK = 64 * 1024


def main() -> int:
    ap = argparse.ArgumentParser(description="TCP throughput to a PUD sink")
    ap.add_argument("host", help="device IP (printed by the device at boot)")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--seconds", type=float, default=10.0,
                    help="how long to push data (default 10 s)")
    ap.add_argument("--threshold-mbps", type=float, default=8.0,
                    help="PASS threshold in Mbit/s (default 8 = 1 MB/s)")
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    payload = b"\xa5" * CHUNK          # 内容无关：对端是纯 sink
    sent = 0
    try:
        sock = socket.create_connection((args.host, args.port),
                                        timeout=args.connect_timeout)
    except OSError as e:
        print(f"ENVIRONMENT_ERROR: cannot connect to {args.host}:{args.port}: {e}",
              file=sys.stderr)
        return 3

    sock.settimeout(args.connect_timeout)
    t0 = time.monotonic()
    deadline = t0 + args.seconds
    try:
        while time.monotonic() < deadline:
            sent += sock.sendall(payload) or len(payload)
    except socket.timeout:
        print("TIMEOUT: send stalled", file=sys.stderr)
        return 4
    except OSError as e:
        print(f"ENVIRONMENT_ERROR: send failed after {sent} bytes: {e}",
              file=sys.stderr)
        return 3
    finally:
        elapsed = time.monotonic() - t0
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        sock.close()

    mbps = sent * 8 / elapsed / 1e6 if elapsed > 0 else 0.0
    mbs = sent / elapsed / 1e6 if elapsed > 0 else 0.0
    verdict = "PASS" if mbps >= args.threshold_mbps else "FAIL"

    if args.json:
        print(json.dumps({
            "host": args.host, "port": args.port, "bytes": sent,
            "seconds": round(elapsed, 3), "mbit_per_s": round(mbps, 3),
            "mbyte_per_s": round(mbs, 3),
            "threshold_mbit_per_s": args.threshold_mbps, "verdict": verdict,
        }, ensure_ascii=False))
    else:
        print(f"{sent/1e6:.2f} MB in {elapsed:.2f} s => "
              f"{mbs:.2f} MB/s ({mbps:.1f} Mbps)  [threshold {args.threshold_mbps:.1f} Mbps]  {verdict}")

    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
