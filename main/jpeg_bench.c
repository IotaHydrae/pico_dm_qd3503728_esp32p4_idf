/*
 * 硬件 JPEG 解码基准（ESP32-P4 的 esp_driver_jpeg）。
 *
 * 为什么是 JPEG 而不是 QOI：P4 有**硬件 JPEG 解码器**（`SOC_JPEG_CODEC_SUPPORTED`
 * ✓），而 PUD 协议里 `DECODER_TYPE 0/1` 本来就是 JPEG（RP2040 上跑软件 tjpgd /
 * JPEGDEC）⇒ P4 用硬件解码器顶同一个协议槽位，**主机侧一行都不用改** ✓。
 * QOI 是 S3 那种"没有解码器、只能省 CPU"的选择，在 P4 上没意义。
 *
 * 三条**架构性**约束（读驱动源码得到，不是猜的）：
 *
 * 1. 解码器的**输入和输出缓冲都必须是 PSRAM**：`jpeg_decoder_process()` 用
 *    2D-DMA 读写，`jpeg_check_dma2d_buffer()` 强制 16 字节对齐 + 外部 RAM
 *    （`esp_driver_jpeg/jpeg_decode.c` L292-293）。⇒ 嵌入在 flash 里的 JPEG
 *    必须先 memcpy 进 PSRAM 才能解。
 * 2. 输出尺寸会被**补齐到 16 像素边界**（YUV420/422 采样），所以缓冲要按补齐后
 *    的宽高算 —— 480x320 本身都是 16 的倍数，本例不用额外补。
 * 3. i80 那边**支持 PSRAM 源缓冲**（`esp_lcd/i80/esp_lcd_panel_io_i80.c` L569
 *    显式分支 `esp_ptr_external_ram(color)` + `ext_mem_align`）⇒ 解码结果**不需要**
 *    再搬进内部 RAM，可以零拷贝直接喂给面板（本项目要实测这条）。
 *
 * 于是 P4 的显示管线应该是：JPEG 流 → PSRAM 输入缓冲 → 硬件解码 → PSRAM 输出
 * → i80 DMA 直接读走 → 面板。32 MB PSRAM 足够放多个整帧缓冲 ⇒ 双缓冲随便开，
 * 而内部 RAM（空闲约 586 KB）根本放不下两个 307 KB 的整帧。
 */

#include <string.h>

#include "driver/jpeg_decode.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "board_pins.h"
#include "tft.h"

#include "assets/jpeg_assets.h"

static const char *TAG = "jpeg";

#define DEC_W   TFT_LOG_HOR_RES   /* 480 */
#define DEC_H   TFT_LOG_VER_RES   /* 320 */
#define DEC_BYTES (DEC_W * DEC_H * 2)

typedef struct {
	const char *name;
	const uint8_t *data;
	const uint32_t len;
} jpeg_asset_t;

static const jpeg_asset_t s_assets[] = {
	{ "bars q60", jpeg_bars_q60, sizeof(jpeg_bars_q60) },
	{ "xfce q30", jpeg_photo_q30, sizeof(jpeg_photo_q30) },
	{ "xfce q60", jpeg_photo_q60, sizeof(jpeg_photo_q60) },
	{ "xfce q90", jpeg_photo_q90, sizeof(jpeg_photo_q90) },
};
#define ASSET_PHOTO 2 /* 用来上屏的那张 */

static jpeg_decoder_handle_t s_dec;
static uint8_t *s_in;              /* PSRAM：JPEG 码流（2D-DMA 要求外部 RAM） */
static size_t s_in_alloc;
static uint16_t *s_out[3];         /* PSRAM：RGB565 输出（[2] 给静态上屏用） */
static jpeg_dec_rgb_element_order_t s_order = JPEG_DEC_RGB_ELEMENT_ORDER_RGB;

static uint16_t *alloc_psram(size_t bytes)
{
	jpeg_decode_memory_alloc_cfg_t cfg = {
		.buffer_direction = JPEG_DEC_ALLOC_OUTPUT_BUFFER,
	};
	size_t got = 0;
	void *p = jpeg_alloc_decoder_mem(bytes, &cfg, &got);

	if (p == NULL)
		ESP_LOGE(TAG, "no PSRAM for %u B", (unsigned)bytes);
	return p;
}

/* 把 flash 里的码流搬进 PSRAM（约束 1）。 */
static bool stage_input(const uint8_t *data, uint32_t len)
{
	if (len > s_in_alloc) {
		ESP_LOGE(TAG, "asset %u B > input buffer %u B", (unsigned)len,
		         (unsigned)s_in_alloc);
		return false;
	}
	memcpy(s_in, data, len);
	return true;
}

static esp_err_t decode_into(const jpeg_asset_t *a, uint16_t *out)
{
	jpeg_decode_cfg_t cfg = {
		.output_format = JPEG_DECODE_OUT_FORMAT_RGB565,
		.rgb_order = s_order,
		.conv_std = JPEG_YUV_RGB_CONV_STD_BT601,
	};
	uint32_t out_size = 0;
	esp_err_t err;

	if (!stage_input(a->data, a->len))
		return ESP_ERR_INVALID_SIZE;
	err = jpeg_decoder_process(s_dec, &cfg, s_in, a->len, (uint8_t *)out,
	                           DEC_BYTES, &out_size);
	if (err != ESP_OK)
		ESP_LOGE(TAG, "decode %s failed: %s", a->name, esp_err_to_name(err));
	return err;
}

/* 从 RGB565 字里取主色通道：0=R 1=G 2=B */
static int dominant(uint16_t px)
{
	int r = (px >> 11) & 0x1f, g = (px >> 5) & 0x3f, b = px & 0x1f;

	if (r > g && r > b)
		return 0;
	if (g > r && g > b)
		return 1;
	return 2;
}

/* 色条图自检：四个象限的中心应该是 红 / 绿 / 蓝 / 白。
 * 这条**不需要人眼**就能判：分数 4 表示元素序正确，2 或更低表示不对。
 * 白象限的主色通道没有意义，只检查它"三个通道都很亮"。 */
static int bars_score(const uint16_t *img)
{
	static const int want[] = { 0, 1, 2 }; /* 左上红、右上绿、左下蓝 */
	const int qx[3] = { DEC_W / 4, 3 * DEC_W / 4, DEC_W / 4 };
	const int qy[3] = { DEC_H / 4, DEC_H / 4, 3 * DEC_H / 4 };
	uint16_t br = img[(3 * DEC_H / 4) * DEC_W + 3 * DEC_W / 4];
	int i, score = 0;

	for (i = 0; i < 3; i++) {
		uint16_t px = img[qy[i] * DEC_W + qx[i]];

		if (dominant(px) == want[i])
			score++;
	}
	if (((br >> 11) & 0x1f) > 20 && ((br >> 5) & 0x3f) > 40 && (br & 0x1f) > 20)
		score++;
	return score;
}

static void detect_element_order(void)
{
	const jpeg_asset_t *bars = &s_assets[0];
	int score_rgb, score_bgr;
	jpeg_decode_cfg_t cfg = { 0 };
	uint32_t out_size = 0;

	/* 先用 get_info 报一下头信息（顺带验证码流本身是好的） */
	jpeg_decode_picture_info_t info = { 0 };

	if (jpeg_decoder_get_info(bars->data, bars->len, &info) == ESP_OK)
		ESP_LOGI(TAG, "bars: %ux%u, sampling %d", (unsigned)info.width,
		         (unsigned)info.height, (int)info.sample_method);
	else
		ESP_LOGE(TAG, "bars: get_info failed");

	cfg.output_format = JPEG_DECODE_OUT_FORMAT_RGB565;
	cfg.conv_std = JPEG_YUV_RGB_CONV_STD_BT601;

	stage_input(bars->data, bars->len);
	cfg.rgb_order = JPEG_DEC_RGB_ELEMENT_ORDER_RGB;
	if (jpeg_decoder_process(s_dec, &cfg, s_in, bars->len, (uint8_t *)s_out[2],
	                         DEC_BYTES, &out_size) == ESP_OK)
		score_rgb = bars_score(s_out[2]);
	else
		score_rgb = -1;

	cfg.rgb_order = JPEG_DEC_RGB_ELEMENT_ORDER_BGR;
	if (jpeg_decoder_process(s_dec, &cfg, s_in, bars->len, (uint8_t *)s_out[2],
	                         DEC_BYTES, &out_size) == ESP_OK)
		score_bgr = bars_score(s_out[2]);
	else
		score_bgr = -1;

	s_order = (score_bgr > score_rgb) ? JPEG_DEC_RGB_ELEMENT_ORDER_BGR
	                                  : JPEG_DEC_RGB_ELEMENT_ORDER_RGB;
	ESP_LOGI(TAG, "element order: RGB scores %d/4, BGR scores %d/4 => using %s",
	         score_rgb, score_bgr,
	         s_order == JPEG_DEC_RGB_ELEMENT_ORDER_BGR ? "BGR" : "RGB");

	/* 把左上角那个已知的红色像素打出来，颜色不对时能立刻对账 */
	ESP_LOGI(TAG, "bars TL pixel 0x%04x (pure red as RGB565 = 0xF800)",
	         s_out[2][(DEC_H / 4) * DEC_W + DEC_W / 4]);
}

void jpeg_bench_run(void)
{
	jpeg_decode_engine_cfg_t eng = {
		.intr_priority = 0,
		.timeout_ms = 100,
	};
	unsigned i;

	if (jpeg_new_decoder_engine(&eng, &s_dec) != ESP_OK) {
		ESP_LOGE(TAG, "no decoder engine");
		return;
	}
	{
		jpeg_decode_memory_alloc_cfg_t in_cfg = {
			.buffer_direction = JPEG_DEC_ALLOC_INPUT_BUFFER,
		};

		s_in = jpeg_alloc_decoder_mem(64 * 1024, &in_cfg, &s_in_alloc);
	}
	s_out[0] = alloc_psram(DEC_BYTES);
	s_out[1] = alloc_psram(DEC_BYTES);
	s_out[2] = alloc_psram(DEC_BYTES);
	if (!s_in || !s_out[0] || !s_out[1] || !s_out[2]) {
		ESP_LOGE(TAG, "buffer allocation failed (PSRAM is mandatory here)");
		return;
	}
	ESP_LOGI(TAG, "engine up: in %u B, out %d B x3 in PSRAM", (unsigned)s_in_alloc,
	         DEC_BYTES);

	detect_element_order();

	/* 每档质量：量"解码一帧要多久"和"主机要传多少字节" */
	ESP_LOGI(TAG, "--- decode benchmark (480x320 -> RGB565) ---");
	for (i = 0; i < sizeof(s_assets) / sizeof(s_assets[0]); i++) {
		const jpeg_asset_t *a = &s_assets[i];
		uint32_t n = 0;
		int64_t t0, dt;

		if (decode_into(a, s_out[0]) != ESP_OK)
			continue;
		t0 = esp_timer_get_time();
		do {
			decode_into(a, s_out[0]);
			n++;
			dt = esp_timer_get_time() - t0;
		} while (dt < 1000000);
		ESP_LOGI(TAG, "%s | %6u B stream | %5.2f ms/frame | %5.1f fps | "
		              "in %5.2f MB/s | %4.1fx compression",
		         a->name, (unsigned)a->len, (double)dt / 1000 / n,
		         (double)n * 1e6 / dt, (double)n * a->len / dt,
		         (double)DEC_BYTES / a->len);
	}

	/* PSRAM 源直接喂 i80：这条决定能不能零拷贝 + 双缓冲（架构性） */
	{
		uint32_t n = 0;
		int64_t t0, dt;

		decode_into(&s_assets[ASSET_PHOTO], s_out[2]);
		t0 = esp_timer_get_time();
		do {
			tft_video_flush(0, 0, DEC_W - 1, DEC_H - 1, s_out[2], DEC_BYTES);
			n++;
			dt = esp_timer_get_time() - t0;
		} while (dt < 1000000);
		ESP_LOGI(TAG, "flush from PSRAM (zero-copy): %u frames in %lld ms => "
		              "%.1f MB/s, %.2f ms/frame",
		         (unsigned)n, (long long)(dt / 1000),
		         (double)n * DEC_BYTES / dt, (double)dt / 1000 / n);
	}

	/* 串行（解码完再发）vs 乒乓（解码与传输重叠）—— 差值就是双缓冲的价值 */
	{
		const jpeg_asset_t *a = &s_assets[ASSET_PHOTO];
		uint32_t n = 0;
		int64_t t0, dt;

		t0 = esp_timer_get_time();
		do {
			decode_into(a, s_out[0]);
			tft_video_flush(0, 0, DEC_W - 1, DEC_H - 1, s_out[0], DEC_BYTES);
			n++;
			dt = esp_timer_get_time() - t0;
		} while (dt < 1500000);
		ESP_LOGI(TAG, "pipeline serial  : %5.2f ms/frame | %5.1f fps",
		         (double)dt / 1000 / n, (double)n * 1e6 / dt);

		n = 0;
		int64_t dec_us = 0, wait_us = 0, t_a, t_b;
		decode_into(a, s_out[0]);
		tft_async_video_flush(0, 0, DEC_W - 1, DEC_H - 1, s_out[0], DEC_BYTES);
		t0 = esp_timer_get_time();
		do {
			/* 先解码（此时上一笔传输正在跑），再等它完成、再提交这一笔。
			 * 分开计时是为了看清"重叠到底成没成"：若 decode 在传输压力下
			 * 明显变慢（远大于单跑的 1.76 ms），说明**PSRAM 带宽被两边争用**，
			 * 那种情况下再上双核也没用（墙是内存带宽，不是 CPU）。 */
			t_a = esp_timer_get_time();
			decode_into(a, s_out[1]);
			t_b = esp_timer_get_time();
			tft_async_video_wait();        /* 上一笔完成 */
			tft_async_video_flush(0, 0, DEC_W - 1, DEC_H - 1, s_out[1], DEC_BYTES);
			dec_us += t_b - t_a;
			wait_us += esp_timer_get_time() - t_b;

			t_a = esp_timer_get_time();
			decode_into(a, s_out[0]);
			t_b = esp_timer_get_time();
			tft_async_video_wait();
			tft_async_video_flush(0, 0, DEC_W - 1, DEC_H - 1, s_out[0], DEC_BYTES);
			dec_us += t_b - t_a;
			wait_us += esp_timer_get_time() - t_b;

			n += 2;
			dt = esp_timer_get_time() - t0;
		} while (dt < 1500000);
		tft_async_video_wait();
		ESP_LOGI(TAG, "pipeline overlapped: %5.2f ms/frame | %5.1f fps "
		              "(breakdown: decode %.2f ms under load vs %.2f ms idle, "
		              "wait-for-bus %.2f ms)",
		         (double)dt / 1000 / n, (double)n * 1e6 / dt,
		         (double)dec_us / 1000 / n,
		         (double)dt / 1000 / n - (double)dec_us / 1000 / n - (double)wait_us / 1000 / n,
		         (double)wait_us / 1000 / n);
	}
	ESP_LOGI(TAG, "--- jpeg bench done ---");
}

/* 给 main.c 的相位 C 用：已经解好的 xfce 照片（RGB565，在 PSRAM 里） */
const void *jpeg_bench_photo(uint32_t *bytes)
{
	if (bytes)
		*bytes = DEC_BYTES;
	return s_out[2];
}

int jpeg_bench_redecode_photo(void)
{
	if (!s_dec)
		return -1;
	return decode_into(&s_assets[ASSET_PHOTO], s_out[2]) == ESP_OK ? 0 : -1;
}
