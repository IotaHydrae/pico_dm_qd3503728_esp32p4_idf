# 硬件 JPEG 解码：约束、性能与零拷贝上屏

> P4 硬件解码 480x320→RGB565 只要 **1.76 ms/帧**（≈570 fps），比总线能吞的快 2.3 倍；
> 解码**输入/输出缓冲必须在 PSRAM**（2D-DMA 要求），而 **i80 能直接读 PSRAM**
> ⇒ 解码结果零拷贝直上屏，双缓冲可以在 PSRAM 里随便开。

## TL;DR

- PUD 协议里 `DECODER_TYPE 0/1` **本来就是 JPEG**（RP2040 上跑软件 tjpgd/JPEGDEC）
  ⇒ P4 用硬件解码顶**同一个协议槽位**，**主机侧一行都不用改** ✓。
- 解码时间几乎**只跟像素数有关**：码流 2.1 KB → 27.6 KB，时间只从 1.70 涨到 1.84 ms
  ⇒ **主机侧该用高质量**，省的是 USB 带宽不是设备时间。
- q60（11 KB）+ 60 fps = **0.66 MB/s = 5.3 Mbps** ⇒ 连 USB 全速 12 Mbps 都装得下
  （PUD 当年的瓶颈从来不是 USB，是软解）；P4 是 HS 480 Mbps，更没有压力。
- 端到端（解码 + 上屏）**串行 5.72 ms/帧（174.7 fps）** ⇒ 对 60 fps 有 3 倍余量 ✓。
- 未解释项：乒乓只比串行快 5%（详情与数据见下），怀疑与 PSRAM 上的 DMA 并行争用有关，
  **尚未验证**，别当结论用。

## 输出格式与元素序：用合成图自动判定（不靠眼睛）

做法：生成一张 480x320 的四象限纯色图（左红/右上绿/左下蓝/右下白，外圈 8px 黑框），
两种 `rgb_order` 各解一次，按"象限中心像素的主色通道"打分：

```text
element order: RGB scores 1/4, BGR scores 4/4 => using BGR
bars TL pixel 0xf800 (pure red as RGB565 = 0xF800)
```

⇒ 元素序 = `JPEG_DEC_RGB_ELEMENT_ORDER_BGR`（与显示层 `swap_color_bytes=0` +
MADCTL `BGR` 配套）；左上角那个已知红点读出标准纯红值 `0xf800`，几何与颜色一次对账
完成 ✓（**人眼只用来确认照片观感，不用来判颜色序**）。

## 性能（480x320 → RGB565，`esp_driver_jpeg`，内置 `xfce` 桌面截图）

| 图 | 码流 | 解码 | fps | 压缩比 |
|---|---|---|---|---|
| bars q60（自检用） | 2125 B | 1.70 ms | 587 | 145x |
| xfce q30 | 7102 B | 1.73 ms | 577 | 43x |
| xfce q60 | 11048 B | 1.76 ms | 569 | 28x |
| xfce q90 | 27593 B | 1.84 ms | 544 | 11x |

零拷贝验证（同一张图，只改源缓冲位置）：

```text
bus: one 307200 B transfer/frame (内部 RAM 源) => 77.6 MB/s, 3.96 ms/frame
flush from PSRAM (zero-copy)                  => 77.5 MB/s, 3.96 ms/frame
```

同速 ⇒ 解码结果留在 PSRAM 即可，**省掉一整帧 307 KB 的搬运**；且内部 RAM
（空闲约 586 KB）只放得下一块 307 KB 整帧、放不下两块 ⇒ **双缓冲只能靠 PSRAM**。

## 为什么必须这样（约束来自驱动源码，不是猜的）

1. **输入和输出缓冲都必须在 PSRAM**：`jpeg_decoder_process()` 用 2D-DMA 读写，
   `jpeg_check_dma2d_buffer()` 强制 16 字节对齐 + 外部 RAM ⇒ flash 里的码流要先
   `memcpy` 进 PSRAM（码流很小，拷贝无所谓）。细节见
   [general/esp32p4-dma-buffer-placement.md](general/esp32p4-dma-buffer-placement.md)。
2. **输出尺寸会补齐到 16 像素边界**（YUV420/422 采样）；480x320 本身是 16 的倍数，
   本例不用额外补，换分辨率时要按补齐后的尺寸申请。
3. **i80 能读 PSRAM** ⇒ 无需再搬进内部 RAM（这一点**否掉了**从 S3 继承的
   "DMA 源必须在内部 RAM" ✗）。

## 端到端与未解释项

```text
pipeline serial    (解码完再发) :  5.72 ms/frame | 174.7 fps
pipeline overlapped(双缓冲乒乓) :  5.46 ms/frame | 183.1 fps
   分解：解码 1.79 ms（负载下，与单跑 1.76 ms 相同）+ 等总线 3.67 ms
```

按 `max(解码, 总线)` 应该是 ≈4.0 ms/帧，实测 5.46 ⇒ **乒乓几乎没有收益**。
分解数据显示**不是 CPU 争用**（解码在负载下速度不变），但等总线的时间也没减少：
那 1.79 ms 里 DMA 只推进了一点点。

- **推测（未验证）**：JPEG 的 2D-DMA（写 PSRAM，≈171 MB/s）与 i80 GDMA（读 PSRAM，
  77 MB/s）并行时互相拖累，墙在内存侧 ⇒ 若如此，**再加一个核做解码也救不了**。
- 实用结论：**单线程串行 5.7 ms/帧已经够用**（60 fps 预算 16.7 ms），先不为这 5%
  增加复杂度；等真机 USB 链路跑起来后再复测一次这个现象。

## 参考

- 驱动：`components/esp_driver_jpeg/include/driver/jpeg_decode.h`
  （`jpeg_new_decoder_engine` / `jpeg_decoder_get_info` / `jpeg_decoder_process` /
  `jpeg_alloc_decoder_mem`），源码 `jpeg_decode.c` 的缓冲对齐检查（L285-295）。
- 复现：`main/jpeg_bench.c`（开机自动跑一遍并打表）；测试图由
  `scripts/make-jpeg-assets.py` 生成。
