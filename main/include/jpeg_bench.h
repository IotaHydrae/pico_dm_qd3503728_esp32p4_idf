/*
 * 硬件 JPEG 解码基准（P4 的 esp_driver_jpeg）—— 实现见 jpeg_bench.c。
 *
 * 为什么是 JPEG 而不是 QOI：P4 有硬件 JPEG 解码器，而 PUD 协议里
 * `DECODER_TYPE 0/1` 本来就是 JPEG（RP2040 上跑软件 tjpgd/JPEGDEC）⇒
 * P4 上换成硬件解码器，**主机侧不用改** ✓。
 */
#ifndef __PUD_JPEG_BENCH_H
#define __PUD_JPEG_BENCH_H

#include <stdint.h>

/* 引擎/缓冲初始化 + 自检（元素序）+ 逐档解码计时 + 零拷贝上屏计时 +
 * 串行 vs 乒乓对比。全部走串口日志。可重复调用（幂等由实现保证：只初始化一次）。 */
extern void jpeg_bench_run(void);

/* 已经解好、可以上屏的那张照片（RGB565，**在 PSRAM 里**，因为解码器的输出
 * 缓冲必须是 PSRAM；i80 支持 PSRAM 源缓冲，所以不需要再搬进内部 RAM）。 */
extern const void *jpeg_bench_photo(uint32_t *bytes);

/* 重新解一帧到那张缓冲里（相位 C 用它测"解码+上屏"的真实串行速率）。 */
extern int jpeg_bench_redecode_photo(void);

#endif /* __PUD_JPEG_BENCH_H */
