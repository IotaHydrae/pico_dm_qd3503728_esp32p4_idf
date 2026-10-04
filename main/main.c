/*
 * PUD (Pico-USB-Display) device firmware, ESP32-P4 port -- panel + JPEG bring-up.
 *
 * 显示层已点亮、转向、定档（细节见 panel.c 与 notes/ 下的知识库）。这里做自检与基准：
 *   芯片/堆 → 面板初始化 → 总线自检（整帧一笔 / fill-only）→ **硬件 JPEG 解码基准**
 *   → 三个相位各 8 秒交替（A 方向测试图 / B 棋盘 / C 硬件解码出来的照片），
 *   三个相位都在**连续重画** ⇒ WR 一直有波形（示波器随时可量）、屏幕随时可看。
 *
 * USB 主机链路还没接（P4 原生 USB 在 P1 的 MX1.25 4pin 上，等线），所以这里
 * 先用**内置的 JPEG** 验证整条解码→上屏路径；主机接通后换成 EP1 上来的码流即可。
 *
 * 坐标系：本文件里所有坐标都是**逻辑坐标**（旋转之后），rot 1 时是 480x320 ——
 * 与主机协议/decoder 看到的一致（board_pins.h 的 TFT_LOG_*）✓。
 */

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_chip_info.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"

#include "board_pins.h"
#include "jpeg_bench.h"
#include "tft.h"

static const char *TAG = "pud";

/* 每相位 8 秒，够看也够算平均 */
#define PHASE_MS 8000

/* 自检画面的刷法：**整帧一笔**。这不是随便定的 —— 笔长扫描实测每"窗口+一笔"有
 * ≈90~120 µs 的固定开销（IDF i80 驱动的 tx_param 每次都要把所有在飞的事务排空再
 * 自旋等 TRANS_DONE），所以 40 行/笔只剩 36.4 MB/s，整帧一笔能到 77.6 MB/s
 * （20 MHz 时是 29.0 → 39.4）。完整的扫描表在 notes/lcd-transfer-throughput.md。 */
#define FRAME_BYTES TFT_LOG_FRAME_BYTES

/* 测试图的判据（自顶向下 5 条横带）：
 *   0) 红底 + 32px 黑格   —— 且 (0,0) 处有一块 16x16 **白块**
 *   1) 绿底 + 32px 黑格
 *   2) 蓝底 + 32px 黑格
 *   3) 1px 黑白**竖**条纹
 *   4) 1px 黑白**横**条纹
 * 判据：白块在左上角（逻辑原点）、红绿蓝自上而下、条纹方向如上 ⇒ 方向与几何都对。
 * 白块跑到底下/右边 ⇒ MADCTL 镜像位不对；条纹方向反了 ⇒ MV 转置那一支不对。 */
static void fill_test_pattern(uint16_t *buf, int rows)
{
	int x, y;

	for (y = 0; y < rows; y++) {
		int seg = y / 64;

		for (x = 0; x < TFT_LOG_HOR_RES; x++) {
			uint16_t c;

			switch (seg) {
			case 0:
				c = ((x % 32 == 0) || (y % 32 == 0)) ? 0x0000 : 0xF800;
				if (x < 16 && y < 16)
					c = 0xFFFF; /* 逻辑原点标记 */
				break;
			case 1: c = ((x % 32 == 0) || (y % 32 == 0)) ? 0x0000 : 0x07E0; break;
			case 2: c = ((x % 32 == 0) || (y % 32 == 0)) ? 0x0000 : 0x001F; break;
			case 3: c = (x & 1) ? 0xFFFF : 0x0000; break;
			default: c = (y & 1) ? 0xFFFF : 0x0000; break;
			}
			buf[y * TFT_LOG_HOR_RES + x] = c;
		}
	}
}

static void fill_checker(uint16_t *buf, int rows)
{
	int x, y;

	for (y = 0; y < rows; y++)
		for (x = 0; x < TFT_LOG_HOR_RES; x++)
			buf[y * TFT_LOG_HOR_RES + x] =
				((((x >> 2) + (y >> 2)) & 1) ? 0xFFFF : 0x0000);
}

static void log_rate(const char *what, uint32_t frames, int64_t dt)
{
	if (dt > 0 && frames > 0)
		ESP_LOGI(TAG, "%s: %lu frames in %lld ms => %.1f fps, %.2f ms/frame",
		         what, (unsigned long)frames, (long long)(dt / 1000),
		         (double)frames * 1e6 / dt, (double)dt / 1000 / frames);
}

/* 总线自检：**整帧一笔**的纯传输速率，以及"只填不发"的 CPU 成本。
 * 完整的笔长扫描表（10/20/40/80/160/320 行）在 notes/lcd-transfer-throughput.md，这里只留
 * 两个关键点：总线够不够快、CPU 填充贵不贵（两者同量级 ⇒ 必须双缓冲）。 */
static void bus_check(uint16_t *buf)
{
	uint32_t n = 0;
	int64_t t0, dt = 0;

	fill_checker(buf, TFT_LOG_VER_RES);
	t0 = esp_timer_get_time();
	do {
		tft_video_flush(0, 0, TFT_LOG_HOR_RES - 1, TFT_LOG_VER_RES - 1, buf,
		                FRAME_BYTES);
		n++;
		dt = esp_timer_get_time() - t0;
	} while (dt < 1000000);
	ESP_LOGI(TAG, "bus: one %d B transfer/frame => %.1f MB/s, %.2f ms/frame",
	         FRAME_BYTES, (double)n * FRAME_BYTES / dt, (double)dt / 1000 / n);

	n = 0;
	t0 = esp_timer_get_time();
	do {
		fill_checker(buf, TFT_LOG_VER_RES);
		n++;
		dt = esp_timer_get_time() - t0;
	} while (dt < 1000000);
	ESP_LOGI(TAG, "cpu fill-only: %.2f ms/frame (%.1f MB/s if it were the only cost)",
	         (double)dt / 1000 / n, (double)n * FRAME_BYTES / dt);
}

static void phase_pattern(uint16_t *buf)
{
	int64_t t0 = esp_timer_get_time(), dt = 0;
	uint32_t n = 0;

	do {
		fill_test_pattern(buf, TFT_LOG_VER_RES);
		tft_video_flush(0, 0, TFT_LOG_HOR_RES - 1, TFT_LOG_VER_RES - 1, buf,
		                FRAME_BYTES);
		n++;
		dt = esp_timer_get_time() - t0;
	} while (dt < (int64_t)PHASE_MS * 1000);
	log_rate("phase A (orientation test pattern)", n, dt);
}

static void phase_checker(uint16_t *buf)
{
	int64_t t0 = esp_timer_get_time(), dt = 0;
	uint32_t n = 0;

	do {
		fill_checker(buf, TFT_LOG_VER_RES);
		tft_video_flush(0, 0, TFT_LOG_HOR_RES - 1, TFT_LOG_VER_RES - 1, buf,
		                FRAME_BYTES);
		n++;
		dt = esp_timer_get_time() - t0;
	} while (dt < (int64_t)PHASE_MS * 1000);
	log_rate("phase B (checkerboard)", n, dt);
}

/* 相位 C：每一帧都**重新用硬件解码**那张 xfce 照片再上屏 ⇒ 串口上的 fps 就是
 * "解码 + 上屏"的真实串行速率（不含双缓冲）。缓冲区在 PSRAM，i80 直接读它。 */
static void phase_jpeg(void)
{
	/* dt 必须初始化：解码失败会 break 出循环，那时 dt 还没被赋值
	 * （-Werror=maybe-uninitialized 抓到的就是这个失败路径）。 */
	int64_t t0 = esp_timer_get_time(), dt = 0;
	uint32_t n = 0, bytes = 0;
	const void *photo;

	if (jpeg_bench_photo(&bytes) == NULL) {
		ESP_LOGW(TAG, "phase C skipped: no jpeg engine");
		return;
	}
	do {
		if (jpeg_bench_redecode_photo() != 0)
			break;
		photo = jpeg_bench_photo(&bytes);
		tft_video_flush(0, 0, TFT_LOG_HOR_RES - 1, TFT_LOG_VER_RES - 1,
		                (void *)photo, bytes);
		n++;
		dt = esp_timer_get_time() - t0;
	} while (dt < (int64_t)PHASE_MS * 1000);
	log_rate("phase C (hw JPEG decode + flush)", n, dt);
}

void app_main(void)
{
	esp_chip_info_t chip;
	uint16_t *buf;
	uint32_t phase = 0;

	esp_chip_info(&chip);
	ESP_LOGI(TAG, "PUD ESP32-P4 bring-up: %d core(s), chip rev v%d.%d, "
	              "reset reason %d",
	         chip.cores, chip.revision / 100, chip.revision % 100,
	         (int)esp_reset_reason());
	ESP_LOGI(TAG, "heap: internal %u bytes free, PSRAM %u bytes free",
	         (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
	         (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

	if (tft_driver_init() != 0) {
		ESP_LOGE(TAG, "panel init FAILED");
		return;
	}
	/* 方向在 tft_driver_init() 里就设好了（panel.c 用 TFT_ROTATION），这里只报一句 */
	ESP_LOGI(TAG, "logical resolution %dx%d (rot %d, native %dx%d)",
	         TFT_LOG_HOR_RES, TFT_LOG_VER_RES, TFT_ROTATION,
	         TFT_HOR_RES, TFT_VER_RES);

	/* 自检画面的缓冲：整帧、内部 RAM（DMA 源要 DMA 可达；整帧 307 KB，
	 * 内部 RAM 放得下一块、放不下两块 ⇒ 双缓冲只能靠 PSRAM）。 */
	buf = heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
	if (buf == NULL) {
		ESP_LOGE(TAG, "no internal DMA buffer (%d bytes)", FRAME_BYTES);
		return;
	}

	backlight_driver_init();
	backlight_set_level(80);

	bus_check(buf);
	jpeg_bench_run();

	/* 三个相位轮转，各 8 秒，且都在连续重画。每相位开始打一行日志说明
	 * "现在屏幕上是什么"，看串口就知道，不用猜。 */
	for (;;) {
		switch (phase % 3) {
		case 0: phase_pattern(buf); break;
		case 1: phase_checker(buf); break;
		default: phase_jpeg(); break;
		}
		phase++;
	}
}
