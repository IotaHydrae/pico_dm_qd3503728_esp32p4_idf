#!/usr/bin/env python3
"""用户脚本的公共外壳：设备地址、连通性预检、进度显示、退出码。

三个用户脚本（`pud_cast.py` / `pud_video.py` / `pud_image.py`）只写"内容从哪来"，
把"发给谁、怎么报进度、出错怎么退出"都放这里 —— 免得三个脚本各写一遍。

退出码（与工作区 testing skill 一致，全工作区统一）：
    0 成功 / 2 参数或用法错 / 3 环境问题（设备不通、缺 ffmpeg/gi 等）/ 4 超时
用户脚本**不判 PASS/FAIL**：判定是 `tests/` 的事，这里只回答"东西发出去没有"。
"""

import argparse
import sys
import time

import pudnet

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_INVALID_USAGE = 2
EXIT_ENVIRONMENT_ERROR = 3
EXIT_TIMEOUT = 4
EXIT_INCONCLUSIVE = 5


def add_common(ap):
    """三个用户脚本都有的开关。"""
    ap.add_argument("--host", default=None,
                    help="设备地址；不给则用 $PUD_HOST 或 ~/.config/pud/host")
    ap.add_argument("--save-host", action="store_true",
                    help="把 --host 记下来，以后不用再给")
    ap.add_argument("--port", type=int, default=pudnet.UDP_PORT)
    ap.add_argument("--quiet", action="store_true", help="只报错，不报进度")


def host_of(args):
    """解析设备地址；顺带处理 --save-host。地址缺失 = INVALID_USAGE。"""
    host = pudnet.require_host(args.host)
    if args.save_host:
        path = pudnet.save_host(host)
        print(f"已记住设备地址：{host}（{path}）")
    return host


def open_sender(args, host, check_reachable=True):
    """连通性预检 + 建发送器。设备不通是**环境问题**(3)，不是失败。"""
    if check_reachable and not pudnet.probe_host(host):
        print(f"设备 {host} ping 不通：确认板子在同一 WiFi、且跑的是无线投屏固件。",
              file=sys.stderr)
        raise SystemExit(EXIT_ENVIRONMENT_ERROR)
    return pudnet.Sender(host, args.port)


def run_stream(sender, frames, args, label, stop_after_seconds=0.0,
               fps=0.0, pace="none", recv=None):
    """通用发送循环：一帧帧发出去，每秒报一次进度；Ctrl-C 正常收尾。

    `frames` 是产出 JPEG 字节的可迭代对象。`pace` 决定要不要在发送侧限速：

    - `pace="none"`（**投屏用**）：来一帧发一帧，不排队也不额外丢。限速交给采集侧的
      `videorate drop-only`（它是按帧率语义工作的，不会像"按到达时刻丢"那样把一帧里
      **最新的那一帧**丢掉 —— 那等于给直播画面加了延迟 ✗，踩过）。
    - `pace="real"`（放视频用）：按 1/fps 的节奏**等**到时间点再发 ⇒ 3 秒的片子
      真的播 3 秒。限速放这里而不是信 ffmpeg 的 `-re`（实测它偏快 18% ✗）。
    """
    quiet = getattr(args, "quiet", False)
    t0 = time.monotonic()
    t_log, win, last = t0, 0, t0
    period = 1.0 / fps if (fps > 0 and pace == "real") else 0.0
    next_due = t0
    try:
        for jpeg in frames:
            now = time.monotonic()
            if period:
                nap = next_due - now
                if nap > 0:
                    time.sleep(nap)
                next_due += period
            sender.send(jpeg)
            last = time.monotonic()
            win += 1
            if stop_after_seconds and last - t0 >= stop_after_seconds:
                break
            if not quiet and last - t_log >= 1.0:
                # 同时报"收到多少"：帧率上不去时，一眼能分清是源只在画面变化时出帧，
                # 还是我们这边丢掉的（收到 >> 送出 = 采集/链路侧在丢）
                got = f" | 收 {recv['n']}" if recv is not None else ""
                print(f"  {label}: {win / (last - t_log):5.1f} fps | "
                      f"{sender.bytes / (last - t0) / 1e6:.2f} MB/s | "
                      f"帧 {sender.frames - 1}{got}", flush=True)
                t_log, win = last, 0
    except KeyboardInterrupt:
        sender.interrupted = True     # 列表播放（视频/图片轮播）据此停掉整轮
        print()
    finally:
        sender.close()

    total = last - t0
    if sender.frames == 0:
        print(f"{label}：一帧都没发出去（采集侧没起来？看上面日志）", file=sys.stderr)
        return EXIT_FAIL
    tail = ""
    if recv is not None and recv["n"] > sender.frames:
        tail = f"（采集侧收到 {recv['n']} 帧，其中 {recv['n'] - sender.frames} 帧在管道里被丢）"
    if total < 0.5:      # 太快（桩/瞬时源）：除出来的速率没有意义，别报假数
        print(f"{label}结束：{sender.frames} 帧{tail}")
    else:
        print(f"{label}结束：{sender.frames} 帧 / {total:.1f} s "
              f"（{sender.frames / total:.1f} fps，{sender.bytes / total / 1e6:.2f} MB/s）{tail}")
    return EXIT_OK
