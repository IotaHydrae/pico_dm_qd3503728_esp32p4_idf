# ESP-IDF i80 的 pclk 只能整数分频：请求值 ≠ 实际值

> `esp_lcd_panel_io_i80` 的 `pclk_hz` 是**整数分频**的结果，分频基数 = PLL_F160M/2 =
> **80 MHz** ⇒ 请求值被向下取整到 **80/n MHz**；请求 50 MHz 实际得到 **80 MHz**。

## TL;DR

- 实际频率 = `80 MHz / floor(80 MHz / 请求值)`，可用档位只有
  **80 / 40 / 26.7 / 20 / 16 / 13.3 / 11.4 / 10 MHz …**，中间值拿不到。
- 因此"请求 50 MHz"实际比请求更高（80 MHz）——低频正常、这一档异常时，
  先算实际值再怀疑硬件。
- 频率算错会让"屏花/屏白"被误判成信号质量问题；**先确认实际时钟**。

## 机制

```text
esp_lcd/i80/esp_lcd_panel_io_i80.c
  L343  prescale   = bus->resolution_hz / io_config->pclk_hz     // 整数除法
  L364 实际 pclk    = bus->resolution_hz / prescale
  L655 lcd_ll_set_group_clock_coeff(dev, LCD_PERIPH_CLOCK_PRE_SCALE, 0, 0)
       // 源码注释：强制整数分频，分数分频会带来 clock jitter
  L660 resolution_hz = src_clk_hz / LCD_PERIPH_CLOCK_PRE_SCALE

esp_lcd/priv_include/esp_lcd_common.h
  LCD_PERIPH_CLOCK_PRE_SCALE = 2

soc/esp32p4/include/soc/clk_tree_defs.h
  LCD_CLK_SRC_DEFAULT = SOC_MOD_CLK_PLL_F160M = 160 MHz   ⇒ 基数 = 80 MHz
```

## 复现与验证

请求值 → 实际值的换算（ESP32-P4，基线 80 MHz）：

| 请求 | `prescale` | 实际 | 说明 |
|---|---|---|---|
| 20 MHz | 4 | 20 MHz | 一致 |
| 40 MHz | 2 | 40 MHz | 一致 |
| 50 MHz | 1 | **80 MHz** | 实际是请求的 1.6 倍 ✗ |

验证方式（无需示波器即可判定成没成）：**吞吐量随笔长单调上升，整帧一笔的理论值 =
实际频率 × 总线字节数/周期**。实测 ESP32-P4 + 16-bit i80：请求 20/40/50 MHz 时整帧
一笔分别是 39.4 / 77.6 / 150.6 MB/s，各自等于"实际频率 × 2 B"的 98.5% / 97% / 94% ✓，
据此反推实际频率为 20 / 40 / **80** MHz，与上面公式一致。

## 边界与陷阱

- 不同芯片的分频基数不同（取决于 `LCD_CLK_SRC_DEFAULT` 与
  `LCD_PERIPH_CLOCK_PRE_SCALE`），换芯片要重算，不要照搬 80 MHz。
- 想拿中间频率：换时钟源（如 XTAL 40 MHz ⇒ 基数 20 MHz，档位 20/10/6.7/5…）
  或接受取整后的值。ESP-IDF 没有暴露运行时改 pclk 的接口，需要重建 panel IO。
- 记录时钟时**写实际值**，不要只写请求值（否则后人按 50 MHz 去查信号问题，方向就错了）。

## 相关

- [esp-idf-i80-transfer-overhead.md](esp-idf-i80-transfer-overhead.md) —— 每笔传输的固定开销
- 项目内的用法（定档 40 MHz 与 80 MHz 出局）：`notes/panel-ili9488-i80.md`
