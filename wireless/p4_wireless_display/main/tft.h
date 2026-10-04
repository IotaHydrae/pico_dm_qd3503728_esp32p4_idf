/*
 * 显示层对外接口（与 ESP32-S3 项目 tft.h 同名同签名 —— decoder 层要能原样继承）。
 *
 * 契约（AGENTS.md §5，是对上一版隐式契约的显式化）：
 *   - tft_video_flush()  返回时，像素**一定已经送完**；
 *   - tft_async_video_flush() 只提交，tft_async_video_wait() 是**唯一**的显式同步点；
 *   - 源缓冲必须是 **DMA 可访问的内部 RAM**（P4 的 DMA 读不了 PSRAM）；
 *   - 所有 esp_lcd_* 返回值都判，失败就报错并让初始化失败。
 */
#ifndef __PUD_TFT_H
#define __PUD_TFT_H

#include <stdint.h>

extern int tft_driver_init(void);
extern int tft_set_rotation(uint8_t rotation);

/* 把 (xs,ys)-(xe,ye) 这块区域的像素刷到面板；vmem 是 RGB565 小端数组。 */
extern void tft_video_flush(int xs, int ys, int xe, int ye, void *vmem,
                            uint32_t len);
extern void tft_async_video_flush(int xs, int ys, int xe, int ye, void *vmem,
                                  uint32_t len);
extern void tft_async_video_wait(void);

/* 背光 0..100 */
extern void backlight_driver_init(void);
extern void backlight_set_level(uint8_t level);
extern uint8_t backlight_get_level(void);

#endif /* __PUD_TFT_H */
