#!/usr/bin/env python3
"""生成 P4 的 JPEG 基准测试图，并落成 C 头文件（main/assets/jpeg_assets.h）。

两张图各有分工：

- bars_480x320.jpg：**自己会判对错**的合成图 —— 四个象限是纯红/绿/蓝/白，
  外圈 8px 黑框。固件解码后直接读几个已知坐标的像素、看主色通道是谁，
  就能判断解码器输出用哪种元素序（RGB 还是 BGR）以及有没有偏移/缩放。
  靠这个就不需要"看一眼猜颜色"。
- photo_q{30,60,90}：用 PUD 的 assets/xfce.jpg（真实桌面截图，缩到 480x320）
  压三档质量，用来量"压缩率 ↔ 解码时间"的取舍（解码时间基本与质量无关，
  但主机侧要传的字节数差好几倍）。bootlogo.png 太"平"，压不出真实差距。

用法：python3 scripts/make-jpeg-assets.py [--assets DIR] [--out FILE]
"""
import argparse
import io
import os
import sys

from PIL import Image

W, H = 480, 320
QUADRANTS = {
    (0, 0): (255, 0, 0),      # 左上：红
    (1, 0): (0, 255, 0),      # 右上：绿
    (0, 1): (0, 0, 255),      # 左下：蓝
    (1, 1): (255, 255, 255),  # 右下：白
}
BORDER = 8


def make_bars():
    im = Image.new("RGB", (W, H), (0, 0, 0))
    px = im.load()
    for y in range(BORDER, H - BORDER):
        for x in range(BORDER, W - BORDER):
            px[x, y] = QUADRANTS[(x >= W // 2, y >= H // 2)]
    return im


def encode(im, quality, subsampling=2):
    """subsampling=2 ⇒ 4:2:0（PUD 主机侧默认就是这个）"""
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, subsampling=subsampling,
            optimize=True, progressive=False)
    return buf.getvalue()


def emit_header(entries, out_path):
    with open(out_path, "w") as f:
        f.write("/*\n"
                " * 自动生成 —— 不要手改。生成脚本：scripts/make-jpeg-assets.py\n"
                " *\n"
                " * bars 图用于**自检**（纯色象限 ⇒ 解码后能直接判元素序/偏移）；\n"
                " * photo 三档质量用于量压缩率与解码时间的取舍。\n"
                " */\n"
                "#ifndef __PUD_JPEG_ASSETS_H\n"
                "#define __PUD_JPEG_ASSETS_H\n\n"
                "#include <stddef.h>\n#include <stdint.h>\n\n")
        for name, data in entries:
            f.write("/* %s: %u bytes */\n" % (name, len(data)))
            f.write("static const uint8_t %s[] = {\n" % name)
            for i in range(0, len(data), 16):
                f.write("\t" + "".join("0x%02x, " % b for b in data[i:i + 16]).rstrip() + "\n")
            f.write("};\n\n")
        f.write("#endif /* __PUD_JPEG_ASSETS_H */\n")


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    ap.add_argument("--assets", default=os.path.join(root, "..", "Pico-USB-Display", "assets"))
    ap.add_argument("--out", default=os.path.join(root, "main", "assets", "jpeg_assets.h"))
    args = ap.parse_args()

    src = os.path.join(args.assets, "xfce.jpg")
    if not os.path.exists(src):
        sys.exit("missing %s" % src)

    entries = []
    bars = encode(make_bars(), 60)
    entries.append(("jpeg_bars_q60", bars))
    print("bars  q60 : %6d B" % len(bars))

    photo = Image.open(src).convert("RGB")
    if photo.size != (W, H):
        photo = photo.resize((W, H), Image.LANCZOS)
    for q in (30, 60, 90):
        data = encode(photo, q)
        entries.append(("jpeg_photo_q%d" % q, data))
        print("photo q%-3d: %6d B" % (q, len(data)))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    emit_header(entries, args.out)
    print("wrote %s (%d arrays)" % (args.out, len(entries)))


if __name__ == "__main__":
    main()
