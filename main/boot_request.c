/*
 * 一键升级：主机不需要按 BOOT。
 *
 * 本应用若占用内部 USB-OTG，而 ESP32-S3 的 USB-OTG 与
 * USB-Serial-JTAG 共用引脚 ⇒ 应用跑起来后 /dev/ttyACM* 消失，
 * 于是"想烧固件"变成"每次手动按住 BOOT"。
 *
 * 出路是让应用自己配合：它自己的 USB 还活着、UART0 的 RX（CH340 的 TX →
 * GPIO41）也接着 ⇒ 主机只要把 magic 发进来，应用置 RTC 的
 * force-download-boot 位再重启，芯片就进 ROM 下载态，
 * USB-Serial-JTAG 随之回来，烧写照旧走 /dev/ttyACM0。
 *
 * 这个监听**必须放在启动最早处**（见 main.c）：它落在显示的 RD 与 CH340 争用
 * 那次崩溃点之前，所以即使应用正在无限重启，主机也仍能把它请进下载态。
 */
#include <stdint.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/uart.h"
#include "esp_system.h"
#include "esp_log.h"
#include "soc/rtc_cntl_reg.h"
#include "esp_rom_sys.h"

#define PUD_BOOT_MAGIC "PUD-BOOT\n"
#define PUD_BOOT_UART UART_NUM_0
/* 跟随 console 的 UART0 配置，别写死：P4 板上 CH343 接的是哪根脚取决于板子。 */
#ifdef CONFIG_ESP_CONSOLE_UART_RX_GPIO
#define PUD_BOOT_RX_GPIO CONFIG_ESP_CONSOLE_UART_RX_GPIO
#else
#define PUD_BOOT_RX_GPIO 38
#endif
#ifdef CONFIG_ESP_CONSOLE_UART_TX_GPIO
#define PUD_BOOT_TX_GPIO CONFIG_ESP_CONSOLE_UART_TX_GPIO
#else
#define PUD_BOOT_TX_GPIO 37
#endif

static const char *TAG = "bootreq";

static void boot_request_task(void *arg)
{
	const size_t magic = sizeof(PUD_BOOT_MAGIC) - 1;
	char win[sizeof(PUD_BOOT_MAGIC) - 1 + 16];
	uint8_t c;

	memset(win, 0, sizeof(win));
	for (;;) {
		if (uart_read_bytes(PUD_BOOT_UART, &c, 1, pdMS_TO_TICKS(100)) <= 0)
			continue;
		memmove(win, win + 1, sizeof(win) - 1);
		win[sizeof(win) - 1] = (char)c;
		if (memcmp(win + sizeof(win) - magic, PUD_BOOT_MAGIC, magic) != 0)
			continue;
		ESP_LOGI(TAG, "download request: rebooting into the ROM bootloader");
		vTaskDelay(pdMS_TO_TICKS(80)); /* 让这行日志先出去 */
		REG_WRITE(RTC_CNTL_OPTION1_REG, RTC_CNTL_FORCE_DOWNLOAD_BOOT);
		esp_restart();
	}
}

void pud_boot_request_start(void)
{
	const uart_config_t cfg = {
		.baud_rate = 115200,
		.data_bits = UART_DATA_8_BITS,
		.parity = UART_PARITY_DISABLE,
		.stop_bits = UART_STOP_BITS_1,
		.flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
		.source_clk = UART_SCLK_DEFAULT,
	};

	/* console 几乎肯定已经装过 UART0 的 driver，而**抢它的 driver / 改它的引脚
	 * 会把 console 一起弄坏**（上一版就是这么把应用弄死在启动最早处的 ✗）。
	 * 所以：没装才装、装了就用它 —— console 的 RX 已经配在 GPIO41
	 * （CONFIG_ESP_CONSOLE_UART_RX_GPIO ✓），正是要监听的那个脚 ✓。 */
	if (!uart_is_driver_installed(PUD_BOOT_UART) &&
	    uart_driver_install(PUD_BOOT_UART, 256, 0, 0, NULL, 0) != ESP_OK) {
		ESP_LOGW(TAG, "no UART0 driver: download request unavailable");
		return;
	}
	/* 引脚路由与参数**必须无条件执行**：console 装了驱动时若跳过这两句，
	 * RX 就永远不会被接到 GPIO41，magic 一个字节都收不到
	 * （实测：跳过时 8 次 magic 全部无人应答 ✗；执行时日志里出现
	 *  `download request:` ✓）。而"装驱动"才是有破坏性的那一步
	 * （console 正在用 UART0 ✗），所以只有它需要判断。 */
	uart_param_config(PUD_BOOT_UART, &cfg);
	uart_set_pin(PUD_BOOT_UART, PUD_BOOT_TX_GPIO, PUD_BOOT_RX_GPIO,
	             UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
	ESP_LOGI(TAG, "listening for the boot magic on UART0 RX (GPIO%d)",
	         PUD_BOOT_RX_GPIO);
	xTaskCreate(boot_request_task, "bootreq", 2560, NULL,
	            tskIDLE_PRIORITY + 5, NULL);

	/* 复位原因打两次：串口在每次重启的瞬间会丢字节（实测把 "listening for
	 * the boot magic" 截成了 "listening fo" ✗），而这一行又是最关键的一行。
	 * 放在 UART 配置**之后**也是同一个原因：它是"启动后第一行"时最容易丢。
	 * esp_reset_reason() 给 ESP_RST_* 语义（BROWNOUT / TASK_WDT / PANIC / SW…），
	 * esp_rom_get_reset_reason() 给 ROM 侧原始原因，两个一起看。 */
	ESP_LOGW(TAG, "reset reason: %d, rom reason core0: %d",
	         (int)esp_reset_reason(), (int)esp_rom_get_reset_reason(0));
	vTaskDelay(pdMS_TO_TICKS(200));
	ESP_LOGW(TAG, "reset reason (again): %d, rom: %d",
	         (int)esp_reset_reason(), (int)esp_rom_get_reset_reason(0));
}
