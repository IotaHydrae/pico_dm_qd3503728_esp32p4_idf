# 知识库索引

**范围**：ESP32-P4 移植（`pico_dm_qd3503728_esp32p4_idf`）的实测结论与踩坑。
ESP32-S3 那份在 `../pico_dm_qd3503728_esp32s3_idf/notes/`；跨项目可复用的通用结论
放在 `general/`（将来工作区建统一知识库时整体上移）。交接与下一步在 `../HANDOFF.md`。

## 索引

| 文档 | 回答什么问题 |
|---|---|
| [bring-up.md](bring-up.md) | 这块板子 + 工具链能跑起来吗？有哪些**必设**项？参考工程能抄什么？ |
| [panel-ili9488-i80.md](panel-ili9488-i80.md) | ILI9488 16-bit i80 怎么点亮、怎么转向、时钟定多少？ |
| [lcd-transfer-throughput.md](lcd-transfer-throughput.md) | 刷一帧要多久？该用多大的笔？为什么必须双缓冲？ |
| [jpeg-hardware-decode.md](jpeg-hardware-decode.md) | P4 硬件 JPEG 解码多快？缓冲放哪？怎么零拷贝上屏？ |
| [pin-map.md](pin-map.md) | 面板/触模接到哪些 GPIO？冲突审计的结论是什么？ |
| [wifi-over-c6-hosted.md](wifi-over-c6-hosted.md) | 这块板的 WiFi 怎么起来？C6 要刷吗？吞吐多少？ |
| [general/esp-idf-i80-pclk-divider.md](general/esp-idf-i80-pclk-divider.md) | 为什么 `pclk_hz` 请求 50 MHz 实际跑 80 MHz？ |
| [general/esp-idf-i80-transfer-overhead.md](general/esp-idf-i80-transfer-overhead.md) | 为什么小笔刷屏白扔带宽？每笔的固定开销从哪来？ |
| [general/esp32p4-dma-buffer-placement.md](general/esp32p4-dma-buffer-placement.md) | P4 上 DMA 缓冲能放 PSRAM 吗？哪些外设强制要 PSRAM？ |
| [general/esp-hosted-version-pinning.md](general/esp-hosted-version-pinning.md) | 为什么换 host 组件版本会让"通"变"不通"？怎么排？ |

登记（**上游内容，未改动，不为满足预算而重写**）：`../hardware-docs/*.pdf` 是厂商
数据手册/技术参考手册；`main/assets/jpeg_assets.h` 由 `scripts/make-jpeg-assets.py`
生成，改测试图请改脚本后重跑。

## 维护约定

- **首屏给结论**：标题 → `> 一句话结论` → TL;DR，10 秒内能判断"这解决什么、我该做什么"。
- **事实分级**：*已验证*（源码/实测/构建日志支撑）直接陈述；*观察*注明测试条件；
  *假设*显式标"推测/未验证"，**不得写成结论**。
- **信息预算**：简单条目 20–80 行，常规 50–150 行，超 150 行触发压缩审查，超 300 行拆分。
- **更新用合并重写**，不用 `cat >>` 追加"更新于某日"；过时的**结论**删掉，
  历史**测量数据**保留并标注"在配置 X 下测得"。
- **漂移检查**（每次动知识库都做）：运行时事实以**代码/配置为准**逐条核对文档断言
  （默认值、常量、开关、特性是否存在），并给出漂移清单（漂在哪 / 以哪边为准 / 怎么修）。
- 每个被实测否定的假设都要留（`✗`）——那是下次不重复踩的依据。
