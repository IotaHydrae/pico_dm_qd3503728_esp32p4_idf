/*
 * P4 + C6（ESP-Hosted 两芯片方案）链路吞吐探针。
 *
 * 目的只有一个：**在接显示之前，先量清楚"从主机经 WiFi 推数据到 P4"能跑多快、
 * 稳不稳**。显示那条链（i80 面板 + 硬件 JPEG）已经在
 * pico_dm_qd3503728_esp32p4_idf 里验收完毕，这个工程一行都不碰它。
 *
 * 判据（写在前面，避免事后找理由）：
 *   - hosted 报出 C6 的 INIT event（没有它就是 SDIO/配置问题，别去调功能）；
 *   - 连上 AP 并拿到 IP；
 *   - TCP sink 持续吞吐 **≥1 MB/s**（480x320 JPEG q60 @60fps 只需 0.66 MB/s）。
 *
 * 为什么 WiFi 连接自己写而不用 protocol_examples_common：那个组件的
 * example_connect() 在本板上先去做以太网初始化（EMAC 无 PHY ⇒ abort），
 * 而且它在 remote WiFi 路径下没有建默认事件循环，
 * esp_event_handler_register() 直接返回 ESP_ERR_INVALID_STATE(0x103) 后 abort。
 * 这里改成显式三步（netif → event loop → wifi），每一步都能打错误码。
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

static const char *TAG = "sink";

#define SINK_PORT 5001
#define RECV_BUF_BYTES (16 * 1024)

static SemaphoreHandle_t s_got_ip;

/* 扫描并打印可见 AP：用来区分"这个 SSID 在 2.4G 上看不到（例如它是 5G 的）"和
 * "密码/认证方式不对"。C6 只有 2.4 GHz 射频，这一步是必要证据。 */
static void scan_visible_aps(void)
{
	wifi_scan_config_t sc = { .show_hidden = true };
	wifi_ap_record_t *recs;
	uint16_t n = 0, i;

	if (esp_wifi_scan_start(&sc, true) != ESP_OK) {
		ESP_LOGW(TAG, "scan failed to start");
		return;
	}
	esp_wifi_scan_get_ap_num(&n);
	recs = calloc(n ? n : 1, sizeof(*recs));
	if (recs == NULL) {
		ESP_LOGW(TAG, "scan: no memory for %u records", n);
		return;
	}
	if (esp_wifi_scan_get_ap_records(&n, recs) != ESP_OK)
		n = 0;
	ESP_LOGI(TAG, "scan: %u AP(s) visible", n);
	for (i = 0; i < n; i++)
		ESP_LOGI(TAG, "  %-24s rssi %4d dBm  ch %2d  auth %d",
		         (const char *)recs[i].ssid, recs[i].rssi, recs[i].primary,
		         (int)recs[i].authmode);
	free(recs);
}


static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id,
                          void *data)
{
	(void)arg;
	if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
		scan_visible_aps();
		ESP_LOGI(TAG, "connecting to \"%s\"", CONFIG_PROBE_WIFI_SSID);
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

/* netif → 事件循环 → wifi：三步都显式，返回值都判（不放在 ESP_ERROR_CHECK 里，
 * 这样失败时会打出是哪个调用、什么错误码，而不是直接 abort）。 */
static esp_err_t wifi_start_sta(void)
{
	wifi_config_t wc = { 0 };
	wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
	esp_err_t err;

	err = esp_netif_init();
	if (err != ESP_OK)
		return err;
	err = esp_event_loop_create_default();
	if (err != ESP_OK)
		return err;
	esp_netif_create_default_wifi_sta();

	err = esp_wifi_init(&init);
	if (err != ESP_OK)
		return err;
	err = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
	                                          on_wifi_event, NULL, NULL);
	if (err != ESP_OK)
		return err;
	err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
	                                          on_wifi_event, NULL, NULL);
	if (err != ESP_OK)
		return err;

	strlcpy((char *)wc.sta.ssid, CONFIG_PROBE_WIFI_SSID, sizeof(wc.sta.ssid));
	strlcpy((char *)wc.sta.password, CONFIG_PROBE_WIFI_PASSWORD,
	        sizeof(wc.sta.password));
	wc.sta.threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK;

	err = esp_wifi_set_mode(WIFI_MODE_STA);
	if (err != ESP_OK)
		return err;
	err = esp_wifi_set_config(WIFI_IF_STA, &wc);
	if (err != ESP_OK)
		return err;
	/* 吞吐优先：关掉省电（省电会让 RX 变成周期性醒来收包）。 */
	(void)esp_wifi_set_ps(WIFI_PS_NONE);
	return esp_wifi_start();
}

/* 收到数据的"纯字节流"吞吐（不做任何协议解析），每秒报一次窗口速率，
 * 连接结束时再报整条连接的均值 —— 两者都留着，因为窗口速率能看出抖动。 */
static void tcp_sink_task(void *arg)
{
	static uint8_t buf[RECV_BUF_BYTES];
	struct sockaddr_in addr = {
		.sin_family = AF_INET,
		.sin_port = htons(SINK_PORT),
		.sin_addr.s_addr = htonl(INADDR_ANY),
	};
	int lfd, rb = 128 * 1024;

	(void)arg;
	lfd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	if (lfd < 0) {
		ESP_LOGE(TAG, "socket: %s", strerror(errno));
		return;
	}
	(void)setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &rb, sizeof(rb));
	if (bind(lfd, (struct sockaddr *)&addr, sizeof(addr)) < 0 ||
	    listen(lfd, 1) < 0) {
		ESP_LOGE(TAG, "bind/listen: %s", strerror(errno));
		close(lfd);
		return;
	}
	ESP_LOGI(TAG, "TCP sink listening on port %d", SINK_PORT);

	for (;;) {
		char peer[32] = "?";
		struct sockaddr_in from;
		socklen_t flen = sizeof(from);
		int cfd = accept(lfd, (struct sockaddr *)&from, &flen);
		uint64_t total = 0, window = 0;
		int64_t t0, tlog;

		if (cfd < 0) {
			ESP_LOGE(TAG, "accept: %s", strerror(errno));
			continue;
		}
		inet_ntoa_r(from.sin_addr, peer, sizeof(peer));
		/* 大接收缓冲：让突发先落在 socket 里，测的才是持续吞吐而不是瞬时抖动。 */
		(void)setsockopt(cfd, SOL_SOCKET, SO_RCVBUF, &rb, sizeof(rb));
		ESP_LOGI(TAG, "client %s connected", peer);

		t0 = tlog = esp_timer_get_time();
		for (;;) {
			int n = recv(cfd, buf, sizeof(buf), 0);
			int64_t now;

			if (n <= 0) {
				ESP_LOGI(TAG, "client %s closed (recv=%d, errno=%d)",
				         peer, n, errno);
				break;
			}
			total += (uint64_t)n;
			window += (uint64_t)n;
			now = esp_timer_get_time();
			if (now - tlog >= 1000000) {
				double dt = (double)(now - tlog) / 1e6;

				ESP_LOGI(TAG, "%.2f MB/s (%.1f Mbps) | total %.2f MB",
				         (double)window / dt / 1e6,
				         (double)window * 8.0 / dt / 1e6,
				         (double)total / 1e6);
				window = 0;
				tlog = now;
			}
		}
		{
			double dt = (double)(esp_timer_get_time() - t0) / 1e6;

			if (dt > 0)
				ESP_LOGI(TAG, "connection summary: %.2f MB in %.2f s => "
				              "%.2f MB/s (%.1f Mbps)",
				         (double)total / 1e6, dt, (double)total / dt / 1e6,
				         (double)total * 8.0 / dt / 1e6);
		}
		close(cfd);
	}
}

void app_main(void)
{
	wifi_ap_record_t ap;
	esp_err_t err;

	ESP_LOGI(TAG, "IDF %s on %s", esp_get_idf_version(), CONFIG_IDF_TARGET);

	s_got_ip = xSemaphoreCreateBinary();
	err = wifi_start_sta();
	if (err != ESP_OK) {
		ESP_LOGE(TAG, "wifi bring-up failed: %s (0x%x)",
		         esp_err_to_name(err), err);
		return;
	}
	if (xSemaphoreTake(s_got_ip,
	                   pdMS_TO_TICKS(CONFIG_PROBE_WIFI_IP_TIMEOUT_S * 1000)) != pdTRUE) {
		ESP_LOGE(TAG, "no IP within %d s", CONFIG_PROBE_WIFI_IP_TIMEOUT_S);
		return;
	}
	if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK)
		ESP_LOGI(TAG, "AP info: ssid=%s rssi=%d dBm channel=%d",
		         ap.ssid, ap.rssi, ap.primary);

	xTaskCreate(tcp_sink_task, "tcp_sink", 4096, NULL, 5, NULL);
}
