/*
 * ILI9488 (QD3503728 扩展板) 的显示层 —— 自己写在 IDF esp_lcd 之上，
 * 不依赖 LovyanGFX（原因见 AGENTS.md §5）。
 *
 * 总线：8080 16-bit 并口，引脚见 board_pins.h（由 notes/pin-map.md 定案）。
 * 初始化序列取自 Pico-USB-Display 的
 *   lib/pico-display-lib/drivers/display/tft_ili9488.c
 * —— 那是这块扩展板上**已经跑通过**的序列，不是猜的 ✓。
 */

#include <string.h>

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_lcd_io_i80.h"
#include "esp_lcd_panel_io.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "board_pins.h"
#include "tft.h"

static const char *TAG = "panel";

/* 颜色字节序：16-bit i80 上 ILI9488 通常需要交换高低字节。若点亮的画面
 * 颜色不对（红蓝互换/花），把这里翻一下即可 —— 这是唯一一个"需要上板确认"
 * 的开关。 */
/* 上板实测：=1 时颜色顺序被"高低字节交换"置换了一遍（白↔白、黄↔紫、
 * 天蓝↔橙黄、绿↔红紫、红↔蓝…），逐项对上 ⇒ 应该是 0 ✗→✓ */
#define PANEL_SWAP_COLOR 0

/* 一次 DMA 传输的最大字节数：按 40 行一条带算，够用且不会吃掉太多内部 RAM。 */
/* 方向：走 board_pins.h 的 TFT_ROTATION（=1，横屏 480x320）。
 * 隔离实验时期这里曾是 0（原生竖屏），上板确认过 rot 0 下图案完全正确 ✓
 * （红/绿/蓝三段 + 三段细条纹自左向右排开，位置与顺序都对得上）⇒ 总线、
 * 时序、初始化序列都没问题，剩下的只是方向换算。 */
#define PANEL_ROTATION TFT_ROTATION

/* 写时钟 —— 注意**请求值不等于实际值**：i80 的 pclk 只能整数分频，
 * 分频基数 = 时钟源/2 = PLL160M/2 = **80 MHz**
 * （IDF esp_lcd/i80/esp_lcd_panel_io_i80.c: L343 `prescale = resolution / pclk`、
 *  L660 `resolution = src_clk_hz / 2`；priv_include/esp_lcd_common.h
 *  `LCD_PERIPH_CLOCK_PRE_SCALE = 2`，且 L655 明确"强制整数分频，分数分频会抖动"）。
 * 于是请求值被**向下取整到 80/n MHz**：
 *   请求 20 MHz → 20（n=4）；请求 40 MHz → 40（n=2）；请求 50 MHz → **80**（n=1）✗
 * 这条是实测反推出来的：50 MHz 请求下整帧一笔跑到 150.6 MB/s ≈ 80 MHz×2 B 的 94%
 * —— 也就是说当初那个"油画感随机色块"的真身是 **80 MHz**，不是 50 MHz ✗。
 * 40 MHz 是这一档的性价比上限（实测 77.6 MB/s = 80 MB/s 的 97%）✓。 */
#define PANEL_BUS_RESOLUTION_HZ 80000000
/* 请求值只有一处定义（board_pins.h 的 TFT_BUS_CLK_KHZ），别在这里再写一份 ——
 * 两份曾经不一致（这里 40000、那里 50000），而 50000 会被上报给主机 ✗。 */
#define PANEL_BUS_CLK_KHZ       TFT_BUS_CLK_KHZ

#define PANEL_BAND_LINES 40
/* 单笔最大长度 == **整屏逻辑帧的字节数**。
 * 上一版这里只给 40 行（38400 B），于是"整屏一笔"会复位 —— 但那**不是
 * LCD_CAM 的硬件上限**：`esp_lcd_panel_io_tx_color()` 会拿总线配置里的
 * `max_transfer_bytes` 校验，超了直接返回 `ESP_ERR_INVALID_ARG`，而当时那行
 * 用 `ESP_ERROR_CHECK` 包着 ⇒ abort ⇒ 看起来像硬件复位 ✗。
 * 现在把上限抬到整屏，让"单笔到底能多大"变成一个**可以测**的问题
 * （main.c 的笔长扫描就是为它做的）。 */
#define PANEL_MAX_XFER   (TFT_LOG_FRAME_BYTES)

/* 完成信号量：esp_lcd 的 i80 IO 是异步的，"等这一笔送完"要靠回调发信号
 * （IDF 没有 wait_idle 这类同步接口）。这样 tft_async_video_wait() 才有一个
 * **显式**的同步点，而不是上一版那种"下一次 setAddrWindow 会顺手等"的隐式契约。 */
static SemaphoreHandle_t s_done;

static bool on_color_done(esp_lcd_panel_io_handle_t io,
                          esp_lcd_panel_io_event_data_t *edata, void *ctx)
{
	BaseType_t hpw = pdFALSE;

	(void)io;
	(void)edata;
	xSemaphoreGiveFromISR((SemaphoreHandle_t)ctx, &hpw);
	return hpw == pdTRUE;
}

static esp_lcd_i80_bus_handle_t s_bus;
static esp_lcd_panel_io_handle_t s_io;
static uint8_t s_rotation = PANEL_ROTATION;

/* 面板命令 */
#define ILI9488_SLPOUT  0x11
#define ILI9488_DISPON  0x29
#define ILI9488_CASET   0x2A
#define ILI9488_PASET   0x2B
#define ILI9488_RAMWR   0x2C
#define ILI9488_MADCTL  0x36
#define ILI9488_COLMOD  0x3A

/* 请求值 → 实际值（整数分频，与驱动同一套算法） */
static uint32_t bus_clk_actual_khz(void)
{
	uint32_t prescale = PANEL_BUS_RESOLUTION_HZ / (PANEL_BUS_CLK_KHZ * 1000);

	return prescale ? PANEL_BUS_RESOLUTION_HZ / prescale / 1000 : PANEL_BUS_CLK_KHZ;
}

static esp_err_t wr_cmd(uint8_t cmd, const uint8_t *params, size_t n)
{
	return esp_lcd_panel_io_tx_param(s_io, cmd, params, n);
}

static uint16_t madctl_for(uint8_t rot)
{
	const uint16_t BGR = 0x08, MX = 0x40, MY = 0x80, MV = 0x20;

	switch (rot) {
	case 0: return MX | BGR;
	case 1: return MV | BGR;
	case 2: return MY | BGR;
	default: return MX | MY | MV | BGR;
	}
}

int tft_set_rotation(uint8_t rotation)
{
	esp_err_t err;

	if (rotation > 3)
		return -1;
	s_rotation = rotation;
	err = wr_cmd(ILI9488_MADCTL, (const uint8_t[]){ (uint8_t)(madctl_for(rotation) & 0xff), }, 1);
	if (err != ESP_OK) {
		ESP_LOGE(TAG, "MADCTL failed: %s", esp_err_to_name(err));
		return -1;
	}
	return 0;
}

int tft_driver_init(void)
{
	esp_err_t err;
	int i;
	const uint8_t d0 = TFT_PIN_D0;  /* 下面这张表按 board_pins.h 展开 */

	ESP_LOGI(TAG, "i80 bus: D0-D15=%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d "
	              "WR=%d DC=%d CS=%d",
	         TFT_PIN_D0, TFT_PIN_D1, TFT_PIN_D2, TFT_PIN_D3, TFT_PIN_D4, TFT_PIN_D5,
	         TFT_PIN_D6, TFT_PIN_D7, TFT_PIN_D8, TFT_PIN_D9, TFT_PIN_D10, TFT_PIN_D11,
	         TFT_PIN_D12, TFT_PIN_D13, TFT_PIN_D14, TFT_PIN_D15,
	         TFT_PIN_WR, TFT_PIN_RS, TFT_PIN_CS);
	(void)d0;

	/* 面板硬复位。ILI9488 需要在收命令前看到 RESX 的一个低脉冲，少了它控制器
	 * 会保持默认状态 —— 实测表现就是**整屏白** ✗（LovyanGFX 那版是靠 cfg.pin_rst
	 * 自己管的，我写 esp_lcd 这版最初漏了）。 */
	{
		gpio_config_t rst = {
			.pin_bit_mask = 1ULL << TFT_PIN_RES,
			.mode = GPIO_MODE_OUTPUT,
		};
		ESP_ERROR_CHECK(gpio_config(&rst));
		gpio_set_level(TFT_PIN_RES, 0);
		vTaskDelay(pdMS_TO_TICKS(20));
		gpio_set_level(TFT_PIN_RES, 1);
		vTaskDelay(pdMS_TO_TICKS(120));
		ESP_LOGI(TAG, "panel reset pulse on GPIO%d done", TFT_PIN_RES);
	}

	esp_lcd_i80_bus_config_t bus_cfg = {
		.clk_src = LCD_CLK_SRC_DEFAULT,
		.dc_gpio_num = TFT_PIN_RS,
		.wr_gpio_num = TFT_PIN_WR,
		.data_gpio_nums = {
			TFT_PIN_D0,  TFT_PIN_D1,  TFT_PIN_D2,  TFT_PIN_D3,
			TFT_PIN_D4,  TFT_PIN_D5,  TFT_PIN_D6,  TFT_PIN_D7,
			TFT_PIN_D8,  TFT_PIN_D9,  TFT_PIN_D10, TFT_PIN_D11,
			TFT_PIN_D12, TFT_PIN_D13, TFT_PIN_D14, TFT_PIN_D15,
		},
		.bus_width = 16,
		.max_transfer_bytes = PANEL_MAX_XFER + 4096,
	};
	err = esp_lcd_new_i80_bus(&bus_cfg, &s_bus);
	if (err != ESP_OK) {
		/* 这一步在 S3 那版被 LovyanGFX 直接丢弃了返回值，结果"日志成功、
		 * 硬件没配" ✗ —— 这里必须让它失败得清清楚楚。 */
		ESP_LOGE(TAG, "esp_lcd_new_i80_bus failed: %s", esp_err_to_name(err));
		return -1;
	}

	s_done = xSemaphoreCreateBinary();
	if (s_done == NULL) {
		ESP_LOGE(TAG, "no semaphore");
		return -1;
	}
	esp_lcd_panel_io_i80_config_t io_cfg = {
		.on_color_trans_done = on_color_done,
		.user_ctx = s_done,
		.cs_gpio_num = TFT_PIN_CS,
		.pclk_hz = PANEL_BUS_CLK_KHZ * 1000,
		.trans_queue_depth = 4,
		/* 命令/参数单位必须是 **8 位**：改成 16 位后，初始化序列里的参数会按
		 * 16 位字发出，面板收到双倍参数 ⇒ 整段初始化失效、屏幕没有任何有意义
		 * 图像 ✗（实测）。这一位与"每周期走几字节"是两件事，别混。 */
		.lcd_cmd_bits = 8,
		.lcd_param_bits = 8,
		.dc_levels = {
			.dc_idle_level = 0,
			.dc_cmd_level = 0,
			.dc_dummy_level = 0,
			.dc_data_level = 1,
		},
		.flags = { .swap_color_bytes = PANEL_SWAP_COLOR },
	};
	err = esp_lcd_new_panel_io_i80(s_bus, &io_cfg, &s_io);
	if (err != ESP_OK) {
		ESP_LOGE(TAG, "esp_lcd_new_panel_io_i80 failed: %s", esp_err_to_name(err));
		return -1;
	}

	/* 把数据线/WR 的驱动能力统一到最低档（≈10 mA）。
	 * 起因：P4 上 GPIO24/25 默认 40 mA，其它脚 20 mA，而 D14/D15 正好是
	 * GPIO24/25，于是这两根线的
	 * 边沿比其它 14 根快，50 MHz（20 ns 周期）下这份额外偏斜会吃掉面板的
	 * setup/hold 余量，表现就是随机的"油画感"色块 ✗。拉平之后再试更高时钟。 */
	{
		static const int pins[] = {
			TFT_PIN_D0, TFT_PIN_D1, TFT_PIN_D2, TFT_PIN_D3,
			TFT_PIN_D4, TFT_PIN_D5, TFT_PIN_D6, TFT_PIN_D7,
			TFT_PIN_D8, TFT_PIN_D9, TFT_PIN_D10, TFT_PIN_D11,
			TFT_PIN_D12, TFT_PIN_D13, TFT_PIN_D14, TFT_PIN_D15,
			TFT_PIN_WR, TFT_PIN_RS, TFT_PIN_CS,
		};
		size_t k;

		for (k = 0; k < sizeof(pins) / sizeof(pins[0]); k++)
			gpio_set_drive_capability((gpio_num_t)pins[k], GPIO_DRIVE_CAP_1);
		/* 注意别把档位和电流写反：CAP_1 是**最低**档（≈10 mA）。 */
		ESP_LOGI(TAG, "drive strength: %u pins set to GPIO_DRIVE_CAP_1 (~10 mA)",
		         (unsigned)k);
	}

	/* ---- ILI9488 初始化（PUD 那份已跑通的序列）---- */
	ESP_RETURN_ON_ERROR(wr_cmd(0xE0, (const uint8_t[]){0x00,0x03,0x09,0x08,0x16,0x0A,0x3F,0x78,0x4C,0x09,0x0A,0x08,0x16,0x1A,0x0F}, 15), TAG, "gamma+");
	ESP_RETURN_ON_ERROR(wr_cmd(0xE1, (const uint8_t[]){0x00,0x16,0x19,0x03,0x0F,0x05,0x32,0x45,0x46,0x04,0x0E,0x0D,0x35,0x37,0x0F}, 15), TAG, "gamma-");
	ESP_RETURN_ON_ERROR(wr_cmd(0xC0, (const uint8_t[]){0x17,0x15}, 2), TAG, "pwr1");
	ESP_RETURN_ON_ERROR(wr_cmd(0xC1, (const uint8_t[]){0x41}, 1), TAG, "pwr2");
	ESP_RETURN_ON_ERROR(wr_cmd(0xC5, (const uint8_t[]){0x00,0x12,0x80}, 3), TAG, "vcom");
	ESP_RETURN_ON_ERROR(wr_cmd(ILI9488_COLMOD, (const uint8_t[]){0x55}, 1), TAG, "colmod"); /* RGB565 8080 16-bit */
	ESP_RETURN_ON_ERROR(wr_cmd(0xB0, (const uint8_t[]){0x00}, 1), TAG, "ifmode");
	ESP_RETURN_ON_ERROR(wr_cmd(0xB1, (const uint8_t[]){0xD0,0x14}, 2), TAG, "frate");
	ESP_RETURN_ON_ERROR(wr_cmd(0xB4, (const uint8_t[]){0x02}, 1), TAG, "inv");
	ESP_RETURN_ON_ERROR(wr_cmd(0xB6, (const uint8_t[]){0x02,0x02,0x3B}, 3), TAG, "dfunc");
	ESP_RETURN_ON_ERROR(wr_cmd(0xB7, (const uint8_t[]){0xC6}, 1), TAG, "entry");
	ESP_RETURN_ON_ERROR(wr_cmd(0xF7, (const uint8_t[]){0xA9,0x51,0x2C,0x82}, 4), TAG, "adj3");
	if (tft_set_rotation(s_rotation) != 0)
		return -1;
	ESP_RETURN_ON_ERROR(wr_cmd(ILI9488_SLPOUT, NULL, 0), TAG, "slpout");
	vTaskDelay(pdMS_TO_TICKS(120));
	ESP_RETURN_ON_ERROR(wr_cmd(ILI9488_DISPON, NULL, 0), TAG, "dispon");
	vTaskDelay(pdMS_TO_TICKS(20));

	ESP_LOGI(TAG, "panel init done: native %dx%d, logical %dx%d (rot %d), "
	              "pclk requested %d kHz -> actual %d kHz, swap_color=%d",
	         TFT_HOR_RES, TFT_VER_RES, TFT_LOG_HOR_RES, TFT_LOG_VER_RES,
	         s_rotation, PANEL_BUS_CLK_KHZ, (int)bus_clk_actual_khz(),
	         PANEL_SWAP_COLOR);
	(void)i;
	return 0;
}

/* 窗口换算：**软件里不交换行列** —— 转向由面板的 MADCTL（MV 位）完成。
 * 对控制器来说置 MV 就是"帧存访问转置"：写完一个像素，地址沿着**逻辑 x**
 * 前进一格；所以一笔行优先的 RGB565 数据正好铺满逻辑窗口。于是 CASET 收的是
 * **逻辑 x**（rot 1 时 0..479）、PASET 收的是**逻辑 y**（0..319）。
 * 这份旋转表（0:MX|BGR, 1:MV|BGR, 2:MY|BGR, 3:MX|MY|MV|BGR）与 PUD 那份
 * fbtft 驱动、以及 TFT_eSPI 对 ILI9488 的取值逐位一致 ✓。
 * 逻辑分辨率随旋转变化（TFT_LOG_HOR_RES/VER_RES）；越界只报一次错、不静默
 * 截断 —— decoder 传错坐标时要能立刻看出来。 */
static void set_addr_window(int xs, int ys, int xe, int ye)
{
	uint8_t caset[4] = { (uint8_t)(xs >> 8), (uint8_t)xs,
	                     (uint8_t)(xe >> 8), (uint8_t)xe };
	uint8_t paset[4] = { (uint8_t)(ys >> 8), (uint8_t)ys,
	                     (uint8_t)(ye >> 8), (uint8_t)ye };
	static bool warned;

	if (!warned && (xs < 0 || ys < 0 || xe < xs || ye < ys ||
	                xe >= TFT_LOG_HOR_RES || ye >= TFT_LOG_VER_RES)) {
		warned = true;
		ESP_LOGE(TAG, "window (%d,%d)-(%d,%d) outside logical %dx%d",
		         xs, ys, xe, ye, TFT_LOG_HOR_RES, TFT_LOG_VER_RES);
	}

	wr_cmd(ILI9488_CASET, caset, 4);
	wr_cmd(ILI9488_PASET, paset, 4);
	wr_cmd(ILI9488_RAMWR, NULL, 0);
}

/* 提交一笔。返回值**必须判**（契约），但这里**不 abort**：刷不上屏是"这一帧
 * 丢了"，不该把整机打死 —— 上一版用 ESP_ERROR_CHECK 包着，超长的一笔变成
 * abort，现场看起来像硬件复位，白查了半天 ✗。失败时把字节数和返回值打出来。 */
static bool submit_color(const uint8_t *vmem, uint32_t len)
{
	esp_err_t err = esp_lcd_panel_io_tx_color(s_io, -1, vmem, len);

	if (err != ESP_OK) {
		ESP_LOGE(TAG, "tx_color %u B failed: %s", (unsigned)len,
		         esp_err_to_name(err));
		return false;
	}
	return true;
}

void tft_video_flush(int xs, int ys, int xe, int ye, void *vmem, uint32_t len)
{
	set_addr_window(xs, ys, xe, ye);
	if (!submit_color((const uint8_t *)vmem, len))
		return;
	tft_async_video_wait(); /* 同步版：返回即送完（契约） */
}

void tft_async_video_flush(int xs, int ys, int xe, int ye, void *vmem,
                           uint32_t len)
{
	set_addr_window(xs, ys, xe, ye);
	/* 只提交：esp_lcd 内部排队，调用方必须用 tft_async_video_wait() 显式同步
	 * （契约见 tft.h；上一版把这个契约藏在"下一次 setAddrWindow 会等"里，
	 * 出问题时表现为无限自旋 ✗）。 */
	submit_color((const uint8_t *)vmem, len);
}

void tft_async_video_wait(void)
{
	if (xSemaphoreTake(s_done, pdMS_TO_TICKS(2000)) != pdTRUE)
		ESP_LOGE(TAG, "panel transfer did not complete in time");
}

/* ---------------- 背光（LEDC）---------------- */

static uint8_t s_bl_level;

void backlight_set_level(uint8_t level)
{
	uint32_t duty;

	if (level > 100)
		level = 100;
	s_bl_level = level;
	duty = (uint32_t)((1u << 10) - 1) * level / 100;
	ESP_ERROR_CHECK(ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty));
	ESP_ERROR_CHECK(ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0));
}

uint8_t backlight_get_level(void)
{
	return s_bl_level;
}

void backlight_driver_init(void)
{
	ledc_timer_config_t t = {
		.speed_mode = LEDC_LOW_SPEED_MODE,
		.duty_resolution = LEDC_TIMER_10_BIT,
		.timer_num = LEDC_TIMER_0,
		.freq_hz = 44100,
		.clk_cfg = LEDC_AUTO_CLK,
	};
	ledc_channel_config_t c = {
		.gpio_num = TFT_PIN_BLK,
		.speed_mode = LEDC_LOW_SPEED_MODE,
		.channel = LEDC_CHANNEL_0,
		.timer_sel = LEDC_TIMER_0,
		.duty = 0,
		.hpoint = 0,
	};

	ESP_ERROR_CHECK(ledc_timer_config(&t));
	ESP_ERROR_CHECK(ledc_channel_config(&c));
}
