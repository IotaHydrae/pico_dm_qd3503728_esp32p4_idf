#!/usr/bin/env python3
"""P4 无线显示器的帧**传输库**：分片发送 + 丢帧策略 + 连通性预检。

被 `pud_cast.py` / `pud_video.py` / `pud_image.py`（用户脚本）与
`tests/test_display_stream.py`（测试）共用；本身不做任何判定，只搬事实。

**设备侧契约**（`main/main.c` 的 `udp_frag_hdr_t`，改动要成对改）：

    [u32 frame][u16 idx][u16 cnt][u32 total] + payload(<= 1400 B)

- `frame` 是帧号：设备**一看到更新的帧号就丢弃没收全的旧帧** ⇒ 丢一个分片只丢一帧，
  不会把画面卡死（这也是为什么 UDP 比 TCP 更适合这里）。
- `idx/cnt` 是分片序号/总数，`total` 是该帧 JPEG 的字节数。
- 面板几何 480x320（旋转 1）；JPEG 必须是这个尺寸（脚本负责缩放/加黑边）。
"""

import os
import socket
import struct
import subprocess
import sys

FRAME_W, FRAME_H = 480, 320     # 面板几何（旋转 1，横屏）
UDP_PORT, TCP_PORT = 5002, 5001
PAYLOAD = 1400                  # 分片载荷上限（含 12 B 头仍远小于 MTU）
HDR = struct.Struct("<IHHI")    # frame, idx, cnt, total
DEFAULT_CONFIG = os.path.expanduser("~/.config/pud/host")


def jpegs_from(stream):
    """从 MJPEG 字节流里切出完整 JPEG（按 SOI/EOI 标记，不依赖容器格式）。"""
    buf = bytearray()
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        buf += chunk
        while True:
            s = buf.find(b"\xff\xd8")
            e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
            if s < 0 or e < 0:
                if len(buf) > 4 * 1024 * 1024:      # 找不到边界就丢，避免无限增长
                    del buf[:-1024]
                break
            yield bytes(buf[s:e + 2])
            del buf[:e + 2]


class Sender:
    """把 JPEG 帧分片发到设备；帧号由自己维护，跨重启单调递增。"""

    def __init__(self, host, port=UDP_PORT, payload=PAYLOAD):
        self.host, self.port, self.payload = host, port, payload
        self.frames = 0
        self.bytes = 0
        self.interrupted = False      # 用户按了 Ctrl-C：列表播放要据此停下来
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.connect((host, port))

    def send(self, jpeg):
        """发一帧；返回这一帧的字节数。"""
        cnt = (len(jpeg) + self.payload - 1) // self.payload
        for i in range(cnt):
            chunk = jpeg[i * self.payload:(i + 1) * self.payload]
            self._sock.send(HDR.pack(self.frames & 0xFFFFFFFF, i, cnt, len(jpeg)) + chunk)
        self.frames += 1
        self.bytes += len(jpeg)
        return len(jpeg)

    def close(self):
        self._sock.close()


class TcpSender:
    """TCP 变体：`[u32 len][JPEG]`（设备侧端口 5001）。

    不丢帧，但上限受 TCP 往返/窗口限制（实测 114 fps vs UDP 的 ~150 fps），
    所以实时画面用 UDP；要"一帧不落"的场合（存证、逐帧比对）才用它。
    """

    def __init__(self, host, port=TCP_PORT, timeout=5.0):
        self.host, self.port = host, port
        self.frames = 0
        self.bytes = 0
        self.interrupted = False
        self._sock = socket.create_connection((host, port), timeout=timeout)

    def send(self, jpeg):
        self._sock.sendall(struct.pack("<I", len(jpeg)) + jpeg)
        self.frames += 1
        self.bytes += len(jpeg)
        return len(jpeg)

    def close(self):
        self._sock.close()


def probe_host(host, timeout_s=2.0):
    """设备通不通（一次 ping）。不通是**环境问题**，不是失败。

    `-W` 是"等回包秒数"、`-w` 是整条命令上限，两个都给，免得丢包时拖很久。
    踩过：第一版漏写了 host 参数 ⇒ 永远报"不通" ✗（真跑一次才抓出来）。
    """
    wait = str(int(max(1, round(timeout_s))))
    try:
        rc = subprocess.run(["ping", "-c", "1", "-W", wait, "-w", wait, host],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=timeout_s + 2).returncode
    except (OSError, subprocess.SubprocessError):
        return False
    return rc == 0


def resolve_host(arg=None):
    """设备地址：命令行 > 环境变量 `PUD_HOST` > `~/.config/pud/host`。"""
    if arg:
        return arg
    if os.environ.get("PUD_HOST"):
        return os.environ["PUD_HOST"]
    try:
        with open(DEFAULT_CONFIG) as f:
            host = f.read().strip()
            if host:
                return host
    except OSError:
        pass
    return None


def save_host(host):
    """记住设备地址，下次不用再输（`~/.config/pud/host`）。"""
    os.makedirs(os.path.dirname(DEFAULT_CONFIG), exist_ok=True)
    with open(DEFAULT_CONFIG, "w") as f:
        f.write(host.strip() + "\n")
    return DEFAULT_CONFIG


def require_host(arg):
    """给用户脚本用：拿不到地址就打印怎么给，然后 INVALID_USAGE(2)。"""
    host = resolve_host(arg)
    if host:
        return host
    print("没给设备地址。任选一种：\n"
          "  --host <IP>        本次指定\n"
          "  export PUD_HOST=<IP>\n"
          "  --save-host        把本次 --host 记下来，以后不用再给",
          file=sys.stderr)
    raise SystemExit(2)
