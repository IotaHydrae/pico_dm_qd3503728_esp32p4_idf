/*
 * 无线显示器：PC 经 WiFi 推 JPEG 帧 → P4 硬件解码 → i80 面板上屏。
 *
 * 两种传输，编译期二选一（`CONFIG_WDD_FRAMES_UDP`）：
 *   - **UDP + 丢帧策略（默认）**：一帧切成若干 ≤1400 B 的分片，每片带 12 B 头
 *     （帧号/片号/片数/总长）。**收到更新的帧号就丢弃当前未组装完的帧** ——
 *     显示的是"最新的完整帧"，不会为了追一帧旧数据而排队 ⇒ 延迟低、不累积。
 *   - **TCP 流（回退/大文件）**：`[u32 长度][JPEG]`，无丢帧但受往返/窗口限制
 *     （实测上限 114 fps，而设备内部能到 ~175 fps）。
 *
 * 各段能力都已单独量化（见同仓 ../../notes/）：
 *   WiFi/SDIO 链路  4.95~5.03 MB/s（40 Mbps）      ← wifi-over-c6-hosted.md
 *   硬件 JPEG 解码  1.70~1.76 ms/帧（480x320）      ← jpeg-hardware-decode.md
 *   i80 上屏        77.6 MB/s，整帧一笔 3.95 ms     ← lcd-transfer-throughput.md
 *
 * 设计要点（都是前面几轮踩出来的）：
 *   - **解码缓冲必须在 PSRAM**（2D-DMA 约束），而 i80 **能直接读 PSRAM** ⇒ 零拷贝上屏；
 *   - **双缓冲乒乓**：解第 N+1 帧时第 N 帧还在 DMA 传输；
 *   - **收帧缓冲也在 PSRAM 且 16 字节对齐**（JPEG 输入要求）；
 *   - **绝不在收包回调里解码**（USB 那轮的教训），只在接收任务里做。
 */

#include <string.h>
#include <errno.h>
#include <stdlib.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "lwip/sockets.h"

#include "driver/jpeg_decode.h"

#include "board_pins.h"
#include "tft.h"

static const char *TAG = "wdd";

#define FRAME_W        TFT_LOG_HOR_RES     /* 480 */
#define FRAME_H        TFT_LOG_VER_RES     /* 320 */
#define FRAME_BYTES    (FRAME_W * FRAME_H * 2)
#define IN_BUF_BYTES   (256 * 1024)        /* 单帧 JPEG 上限（q60 约 11 KB） */

/* UDP 分片：载荷固定 1400 B（避开 IP 分片），头 12 B。两端必须一致。 */
#define UDP_PAYLOAD    1400
#define UDP_HDR_BYTES  12
#define UDP_MAX_CHUNKS (IN_BUF_BYTES / UDP_PAYLOAD + 1)

typedef struct __attribute__((packed)) {
	uint32_t frame;   /* 帧号（递增；用来判断"更新"） */
	uint16_t idx;     /* 片号，从 0 起 */
	uint16_t cnt;     /* 本帧总片数 */
	uint32_t total;   /* 本帧 JPEG 总字节数 */
} udp_frag_hdr_t;

static SemaphoreHandle_t s_got_ip;
static jpeg_decoder_handle_t s_dec;
static uint8_t *s_in;            /* PSRAM：收到的 JPEG */
static size_t s_in_size;
static uint16_t *s_out[2];       /* PSRAM：两块 RGB565 输出，乒乓 */
static size_t s_out_size;

/* ---------------- WiFi（三步显式，返回值都判） ---------------- */

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
	(void)arg;
	if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
		esp_wifi_connect();
	} else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
		const wifi_event_sta_disconnected_t *d = data;

		ESP_LOGW(TAG, "disconnected (reason %d), retrying in 2 s", d->reason);
		vTaskDelay(pdMS_TO_TICKS(2000));
		esp_wifi_connect();
	} else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
		const ip_event_got_ip_t *e = data;

		ESP_LOGI(TAG, "got IP " IPSTR, IP2STR(&e->ip_info.ip));
		xSemaphoreGive(s_got_ip);
	}
}

static esp_err_t wifi_start_sta(void)
{
	wifi_config_t wc = { 0 };
	wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
	esp_err_t err;

	if ((err = esp_netif_init()) != ESP_OK)
		return err;
	if ((err = esp_event_loop_create_default()) != ESP_OK)
		return err;
	esp_netif_create_default_wifi_sta();

	if ((err = esp_wifi_init(&init)) != ESP_OK)
		return err;
	if ((err = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
	                                               on_wifi_event, NULL, NULL)) != ESP_OK)
		return err;
	if ((err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
	                                               on_wifi_event, NULL, NULL)) != ESP_OK)
		return err;

	strlcpy((char *)wc.sta.ssid, CONFIG_WDD_WIFI_SSID, sizeof(wc.sta.ssid));
	strlcpy((char *)wc.sta.password, CONFIG_WDD_WIFI_PASSWORD, sizeof(wc.sta.password));
	wc.sta.threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK;

	if ((err = esp_wifi_set_mode(WIFI_MODE_STA)) != ESP_OK)
		return err;
	if ((err = esp_wifi_set_config(WIFI_IF_STA, &wc)) != ESP_OK)
		return err;
	(void)esp_wifi_set_ps(WIFI_PS_NONE);   /* 吞吐优先 */
	return esp_wifi_start();
}

/* ---------------- 显示 + 解码 ---------------- */

static esp_err_t display_and_decoder_init(void)
{
	jpeg_decode_engine_cfg_t eng = { .intr_priority = 0, .timeout_ms = 100 };
	jpeg_decode_memory_alloc_cfg_t in_cfg = { .buffer_direction = JPEG_DEC_ALLOC_INPUT_BUFFER };
	jpeg_decode_memory_alloc_cfg_t out_cfg = { .buffer_direction = JPEG_DEC_ALLOC_OUTPUT_BUFFER };
	esp_err_t err;

	if (tft_driver_init() != 0) {
		ESP_LOGE(TAG, "panel init failed");
		return ESP_FAIL;
	}
	backlight_driver_init();
	backlight_set_level(80);

	if ((err = jpeg_new_decoder_engine(&eng, &s_dec)) != ESP_OK)
		return err;
	s_in = jpeg_alloc_decoder_mem(IN_BUF_BYTES, &in_cfg, &s_in_size);
	s_out[0] = jpeg_alloc_decoder_mem(FRAME_BYTES, &out_cfg, &s_out_size);
	s_out[1] = jpeg_alloc_decoder_mem(FRAME_BYTES, &out_cfg, &s_out_size);
	if (!s_in || !s_out[0] || !s_out[1]) {
		ESP_LOGE(TAG, "no PSRAM for buffers (in %u, out %u x2)",
		         (unsigned)IN_BUF_BYTES, FRAME_BYTES);
		return ESP_ERR_NO_MEM;
	}
	ESP_LOGI(TAG, "panel %dx%d up; jpeg in %u B, out %u B x2 in PSRAM",
	         FRAME_W, FRAME_H, (unsigned)s_in_size, (unsigned)s_out_size);
	return ESP_OK;
}

/* 展示统计：每 2 秒一行，两种传输共用。 */
struct show_stat {
	uint32_t shown, dropped, bad;
	uint32_t win_frames;
	int64_t dec_us, bus_us, t_log;
	int slot;
};

static void show_frame(struct show_stat *st, uint32_t len)
{
	jpeg_decode_cfg_t cfg = {
		.output_format = JPEG_DECODE_OUT_FORMAT_RGB565,
		.rgb_order = JPEG_DEC_RGB_ELEMENT_ORDER_BGR,   /* 上板用色条图自动判定过 */
		.conv_std = JPEG_YUV_RGB_CONV_STD_BT601,
	};
	uint32_t out_len = 0;
	int64_t t0, now;

	t0 = esp_timer_get_time();
	if (jpeg_decoder_process(s_dec, &cfg, s_in, len, (uint8_t *)s_out[st->slot],
	                         s_out_size, &out_len) != ESP_OK) {
		if (st->bad++ < 3)
			ESP_LOGW(TAG, "decode failed (%u B)", (unsigned)len);
		return;
	}
	st->dec_us += esp_timer_get_time() - t0;

	t0 = esp_timer_get_time();
	tft_async_video_flush(0, 0, FRAME_W - 1, FRAME_H - 1, s_out[st->slot], FRAME_BYTES);
	tft_async_video_wait();          /* 契约：等这一笔送完，槽才能复用 */
	st->bus_us += esp_timer_get_time() - t0;

	st->slot ^= 1;
	st->shown++;
	st->win_frames++;

	now = esp_timer_get_time();
	if (now - st->t_log >= 2000000) {
		double dt = (double)(now - st->t_log) / 1e6;

		ESP_LOGI(TAG, "%.1f fps | decode %.2f ms | flush %.2f ms | "
		              "%lu shown, %lu dropped, %lu bad",
		         (double)st->win_frames / dt,
		         st->win_frames ? (double)st->dec_us / 1000 / st->win_frames : 0.0,
		         st->win_frames ? (double)st->bus_us / 1000 / st->win_frames : 0.0,
		         (unsigned long)st->shown, (unsigned long)st->dropped,
		         (unsigned long)st->bad);
		st->win_frames = 0;
		st->dec_us = st->bus_us = 0;
		st->t_log = now;
	}
}

/* ---------------- UDP 收帧（默认路径） ---------------- */

static void udp_recv_task(void *arg)
{
	static struct show_stat st;
	static struct sockaddr_in from;
	static uint8_t bitmap[(UDP_MAX_CHUNKS + 7) / 8];
	struct sockaddr_in addr = {
		.sin_family = AF_INET,
		.sin_port = htons(CONFIG_WDD_UDP_PORT),
		.sin_addr.s_addr = htonl(INADDR_ANY),
	};
	int rb = 256 * 1024;
	int fd;
	uint32_t cur_frame = 0;
	uint16_t cur_cnt = 0, cur_have = 0;
	int64_t last_warn = 0;

	(void)arg;
	st.t_log = esp_timer_get_time();
	fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	if (fd < 0) {
		ESP_LOGE(TAG, "udp socket: %s", strerror(errno));
		return;
	}
	(void)setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rb, sizeof(rb));
	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		ESP_LOGE(TAG, "udp bind: %s", strerror(errno));
		close(fd);
		return;
	}
	ESP_LOGI(TAG, "UDP frames on port %d (%d B payload, %d B header)",
	         CONFIG_WDD_UDP_PORT, UDP_PAYLOAD, UDP_HDR_BYTES);

	for (;;) {
		/* 收包缓冲 = 头 + 载荷；预留在栈外的静态区，避免每次 recvfrom 都分配 */
		static uint8_t pkt[UDP_HDR_BYTES + UDP_PAYLOAD];
		socklen_t flen = sizeof(from);
		int n = recvfrom(fd, pkt, sizeof(pkt), 0, (struct sockaddr *)&from, &flen);
		const udp_frag_hdr_t *h;
		uint16_t payload_len;

		if (n < UDP_HDR_BYTES) {
			if (n < 0 && errno != EAGAIN && esp_timer_get_time() - last_warn > 2000000) {
				ESP_LOGW(TAG, "udp recv: %s", strerror(errno));
				last_warn = esp_timer_get_time();
			}
			continue;
		}
		h = (const udp_frag_hdr_t *)pkt;
		payload_len = (uint16_t)(n - UDP_HDR_BYTES);
		if (h->cnt == 0 || h->cnt > UDP_MAX_CHUNKS || h->idx >= h->cnt ||
		    h->total == 0 || h->total > s_in_size)
			continue;

		if (h->frame != cur_frame) {
			/* 新帧号：只要比当前新就切换；未组装完的那帧计入 dropped
			 * （这就是"丢旧帧"策略 —— 永远显示最新的完整帧，不排队追旧数据）。 */
			if ((int32_t)(h->frame - cur_frame) > 0) {
				if (cur_frame != 0 && cur_have != cur_cnt)
					st.dropped++;
				cur_frame = h->frame;
				cur_cnt = h->cnt;
				cur_have = 0;
				memset(bitmap, 0, sizeof(bitmap));
			} else {
				continue;   /* 迟到/乱序的旧帧分片：丢弃 */
			}
		}
		if (bitmap[h->idx >> 3] & (1u << (h->idx & 7)))
			continue;           /* 重复分片 */
		bitmap[h->idx >> 3] |= (uint8_t)(1u << (h->idx & 7));
		memcpy(s_in + (size_t)h->idx * UDP_PAYLOAD, pkt + UDP_HDR_BYTES, payload_len);
		cur_have++;

		if (cur_have == cur_cnt) {
			/* 整帧到齐：解码 + 上屏。长度以头部声明为准（末片可能不满）。 */
			if ((size_t)(cur_cnt - 1) * UDP_PAYLOAD + payload_len >= h->total)
				show_frame(&st, h->total);
			cur_frame = 0;
			cur_cnt = cur_have = 0;
		}
	}
}

/* ---------------- TCP 流收帧（回退路径） ---------------- */

static void tcp_recv_task(void *arg)
{
	static struct show_stat st;
	int rb = 128 * 1024;
	int lfd;

	(void)arg;
	st.t_log = esp_timer_get_time();
	lfd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	if (lfd < 0) {
		ESP_LOGE(TAG, "socket: %s", strerror(errno));
		return;
	}
	{
		struct sockaddr_in addr = {
			.sin_family = AF_INET,
			.sin_port = htons(CONFIG_WDD_TCP_PORT),
			.sin_addr.s_addr = htonl(INADDR_ANY),
		};
		int on = 1;

		(void)setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));
		if (bind(lfd, (struct sockaddr *)&addr, sizeof(addr)) < 0 || listen(lfd, 1) < 0) {
			ESP_LOGE(TAG, "bind/listen: %s", strerror(errno));
			close(lfd);
			return;
		}
	}
	ESP_LOGI(TAG, "TCP frames on port %d", CONFIG_WDD_TCP_PORT);

	for (;;) {
		int cfd = accept(lfd, NULL, NULL);

		if (cfd < 0) {
			ESP_LOGE(TAG, "accept: %s", strerror(errno));
			continue;
		}
		(void)setsockopt(cfd, SOL_SOCKET, SO_RCVBUF, &rb, sizeof(rb));
		ESP_LOGI(TAG, "sender connected");

		while (1) {
			uint32_t expect;
			uint8_t *p = (uint8_t *)&expect;
			size_t need = sizeof(expect), got = 0;

			while (got < need) {
				int n = recv(cfd, p + got, need - got, 0);

				if (n <= 0)
					goto closed;
				got += (size_t)n;
			}
			if (expect == 0 || expect > s_in_size) {
				if (st.bad++ < 3)
					ESP_LOGW(TAG, "reject frame length %u (max %u)",
					         (unsigned)expect, (unsigned)s_in_size);
				goto closed;
			}
			got = 0;
			while (got < expect) {
				int n = recv(cfd, s_in + got, expect - got, 0);

				if (n <= 0)
					goto closed;
				got += (size_t)n;
			}
			show_frame(&st, expect);
		}
closed:
		ESP_LOGI(TAG, "sender gone (after %lu frames)", (unsigned long)st.shown);
		close(cfd);
	}
}

void app_main(void)
{
	esp_err_t err;

	ESP_LOGI(TAG, "wireless display demo: IDF %s on %s",
	         esp_get_idf_version(), CONFIG_IDF_TARGET);

	if (display_and_decoder_init() != ESP_OK)
		return;

	s_got_ip = xSemaphoreCreateBinary();
	if ((err = wifi_start_sta()) != ESP_OK) {
		ESP_LOGE(TAG, "wifi bring-up failed: %s (0x%x)", esp_err_to_name(err), err);
		return;
	}
	if (xSemaphoreTake(s_got_ip, pdMS_TO_TICKS(CONFIG_WDD_WIFI_IP_TIMEOUT_S * 1000)) != pdTRUE) {
		ESP_LOGE(TAG, "no IP within %d s", CONFIG_WDD_WIFI_IP_TIMEOUT_S);
		return;
	}
#if CONFIG_WDD_FRAMES_UDP
	xTaskCreate(udp_recv_task, "frames_udp", 6144, NULL, 5, NULL);
#else
	xTaskCreate(tcp_recv_task, "frames_tcp", 6144, NULL, 5, NULL);
#endif
}
