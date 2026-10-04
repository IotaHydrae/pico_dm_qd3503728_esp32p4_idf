# ESP-IDF i80：每"窗口 + 一笔"有 ≈90~120 µs 固定开销

> `esp_lcd_panel_io_i80` 的 `tx_param()` 每次调用都会**排空所有在飞事务再自旋等完成**，
> 一个窗口要发 3 条命令 ⇒ 固定开销 ≈90~120 µs/笔，与数据量无关。

## TL;DR

- 症状：整屏吞吐远低于"时钟 × 总线宽度"的理论值，且**笔越小越低**。
- 修法：**能整帧就整帧**；必须分块时用大块（≥半屏），不要按行/小带刷。
- 只有 `tx_color()` 是异步的；`tx_param()` 是**同步且自旋**的 ⇒ 别指望窗口命令能排队。

## 机制（源码定位）

```text
esp_lcd/i80/esp_lcd_panel_io_i80.c
  panel_io_i80_tx_param():
    L504-510  for (num_trans_inflight) xQueueReceive(done_queue, portMAX_DELAY)  // 排空在飞事务
    L552      while (!(lcd_ll_get_interrupt_status(...) & LCD_LL_EVENT_TRANS_DONE)) {}  // 自旋等完成
  panel_io_i80_tx_color():
    L589-611  只把事务塞进 trans_queue 并 esp_intr_enable()，由 ISR 派发（异步）
```

一个"设置窗口 + 送像素"的动作 = 3 次 `tx_param`（CASET/PASET/RAMWR）+ 1 次 `tx_color`
⇒ 约 4 次中断/队列往返 ≈ 90~120 µs，**与像素数量无关**。

## 复现（ESP32-P4 + ILI9488 16-bit i80 @ 40 MHz，480x320 整帧 307200 B）

| 每笔字节 | 每帧笔数 | 实测吞吐 | 相对整帧一笔 |
|---|---|---|---|
| 9600 | 32 | 45.6 MB/s | 59% |
| 19200 | 16 | 57.9 MB/s | 75% |
| 38400 | 8 | 66.9 MB/s | 86% |
| 76800 | 4 | 72.6 MB/s | 94% |
| 153600 | 2 | 75.8 MB/s | 98% |
| **307200** | **1** | **77.6 MB/s** | 100% |

反推固定开销：331 µs（10 行）− 240 µs（数据净时间）= **≈91 µs/笔**；
7.80 ms（整帧）− 7.68 ms = **≈120 µs/笔** ⇒ 与笔长无关的常数 ✓。

## 边界与陷阱

- **吞吐随笔长单调上升**这条也是"时钟是否真的生效"的判据（见
  [esp-idf-i80-pclk-divider.md](esp-idf-i80-pclk-divider.md)）。
- `tx_color()` 有 `color_size <= bus->max_transfer_bytes` 的**自设**校验
  （L567），超了返回 `ESP_ERR_INVALID_ARG`。别用 `ESP_ERROR_CHECK` 包它 ——
  否则"一帧超长"会变成整机 abort，现场看起来像硬件崩溃。
- 传输源缓冲可以是**外部 RAM（PSRAM）**，驱动按 `ext_mem_align` 处理；
  只有内部 RAM 才用 `int_mem_align`（L569-577）。
- IDF v6.1 实测；换大版本请重新核对行号与行为。

## 相关

- [esp32p4-dma-buffer-placement.md](esp32p4-dma-buffer-placement.md)
- 项目内的实测表与刷法结论：`notes/lcd-transfer-throughput.md`
