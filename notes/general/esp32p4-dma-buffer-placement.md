# ESP32-P4：DMA 缓冲放内部 RAM 还是 PSRAM

> **i80 面板的源缓冲可以是 PSRAM**（与内部 RAM 同速，实测 77.5 vs 77.6 MB/s）；
> 但 **JPEG 编解码的输入/输出缓冲必须在 PSRAM**（2D-DMA 限制，且要 16 字节对齐）。

## TL;DR

- 别把"S3 上 DMA 源必须在内部 RAM"照搬到 P4 ✗ —— P4 的 i80 驱动显式支持外部内存。
- 反过来的坑更容易踩：**硬件 JPEG 的缓冲必须在 PSRAM**，放内部 RAM 会直接被拒
  （`jpeg_check_dma2d_buffer`：非 PSRAM / 未 16 字节对齐 ⇒ `ESP_ERR_INVALID_ARG`）。
- 结果：P4 上"解码 → 上屏"可以**零拷贝**（解码输出留在 PSRAM，i80 直接读）。
  内部 RAM 很紧张（约 586 KB 空闲），整帧 307 KB ⇒ 双缓冲只能靠 PSRAM（32 MB）。

## i80：源缓冲允许 PSRAM

```text
esp_lcd/i80/esp_lcd_panel_io_i80.c
  L569-577  esp_ptr_external_ram(color) ? 按 ext_mem_align 校验 : 按 int_mem_align 校验
  L693      gdma_get_alignment_constraints(bus->dma_chan, &int_mem_align, &ext_mem_align)
  L578-581  若缓冲有 cache line，驱动自己做 C2M msync
```

实测（480x320 RGB565 整帧 307200 B，40 MHz，同一份代码只换缓冲位置）：

```text
内部 RAM 源 => 77.6 MB/s, 3.96 ms/frame
PSRAM  源 => 77.5 MB/s, 3.96 ms/frame      // 同速
```

## JPEG：缓冲必须在 PSRAM

```text
esp_driver_jpeg/jpeg_decode.c
  L285-295  jpeg_decoder_process(): 校验 decode_outbuf 与 bit_stream
            "not 16-byte aligned or not in unencrypted PSRAM" ⇒ 直接返回错误
  说明注释：输出缓冲是 2D-DMA 写 PSRAM，输入是 PSRAM 写 2D-DMA，两者都受 2D-DMA 约束
```

实践：

- 用 `jpeg_alloc_decoder_mem(size, {.buffer_direction = ...}, &allocated_size)` 分配，
  它按 `MAX(cache_align, JPEG_DMA2D_BUFFER_ALIGN)` 对齐（也顺手处理了加密 PSRAM 的可用区）。
- **flash 里的常量码流不能直接喂**（既不是 PSRAM 也未必对齐）⇒ 先 `memcpy` 到 PSRAM 缓冲。
- 输出缓冲要按"补齐到 16 像素边界后的宽高"算尺寸（YUV420/422 采样）。

## 边界与陷阱

- 上面所有结论都是 **ESP32-P4 + IDF v6.1** 实测/源码结论；ESP32-S3 的 LCD_CAM
  规则**不同**（S3 上 DMA 源必须内部 RAM），换芯片要重新验证，别跨芯片外推。
- 缓存一致性由驱动负责（i80 的 `esp_cache_msync(C2M)`、JPEG 的输入 C2M / 输出 M2C）
  ⇒ 用官方分配器与官方 API 时不必自己 msync；自己 `malloc` PSRAM 并绕开 API 时要自己管。
- PSRAM 上并行 DMA（如 JPEG 2D-DMA 写 + i80 GDMA 读）**可能互相拖累**；这一点在
  ESP32-P4 上观察到过但**尚未查清**，不要假设"两个 DMA 一定并行不互相影响"。

## 相关

- [esp-idf-i80-transfer-overhead.md](esp-idf-i80-transfer-overhead.md)
- 项目内用法与实测：`notes/jpeg-hardware-decode.md`
