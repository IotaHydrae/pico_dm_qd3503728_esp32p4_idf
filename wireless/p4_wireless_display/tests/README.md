# tests/ —— 验证脚本

每个测试做一件事：**收集观察 → 对照显式 oracle → 给出 PASS / FAIL / INCONCLUSIVE**。
公共外壳在 [`common/harness.py`](common/harness.py)（CLI、退出码、oracle 声明、JSON），
与 `Pico-USB-Display/tests/common/harness.py` **是同一份**（改动要两边同步，像 `skills/` 那样）。
设备访问与测量原语不在这里再实现一遍：帧传输在 [`../tools/pudnet.py`](../tools/pudnet.py)，
门户采集在 [`../tools/portal_capture.py`](../tools/portal_capture.py)，
媒体源在 [`../tools/pud_media.py`](../tools/pud_media.py)。**方向只能是 tests → tools。**

## 退出码（全工作区统一）

```text
0 PASS   1 FAIL   2 INVALID_USAGE   3 ENVIRONMENT_ERROR   4 TIMEOUT   5 INCONCLUSIVE
```

设备 ping 不通 / 缺依赖（Pillow、ffmpeg、gst-launch）= **3**，不是 FAIL。
没有可靠 oracle 的用例报 **5**，不要编阈值。所有测试都支持
`--help / --version / --json / --timeout / --quiet / --verbose`。

## 真机（板子在同一个 WiFi 上，跑无线投屏固件）

```bash
python3 tests/test_display_stream.py --host <设备IP>              # 20 s，PASS/FAIL
python3 tests/test_display_stream.py --host <设备IP> --json        # 机器可读
python3 tests/test_display_stream.py --host <IP> --preencode 90    # 把 PIL 编码成本移出计时
```

用**系统 python**（要 Pillow；IDF 的 python 环境里没有）。设备侧同时能看串口日志：
`wdd: <fps> | decode ... | <shown> shown, <dropped> dropped, <bad> bad`。

## Oracle 来源

| 测试 | oracle | 来源 |
| --- | --- | --- |
| `test_display_stream` | REQUIREMENT | 面板要能"实时看"的验收要求 + 链路实测 `../p4_wifi_probe/FINDINGS.md`（4.95~5.03 MB/s） |

## 写新测试时

1. 先问"这条测试的期望值从哪来"（SPEC / REQUIREMENT / INVARIANT / RELATIONSHIP /
   GOLDEN / BASELINE / USER_DEFINED），**说不出来就用 `NONE` 并报 INCONCLUSIVE**。
2. 观察与判定分开：观察进 `Snapshot`，判定交给`Report.of(..., ok=...)`。
3. 判定逻辑别写在 `main()` 里 —— 写成一个纯函数，这样离线的桩测试才能覆盖它
   （`Pico-USB-Display/tests/test_runner_contract.py` 就是这么保护真机用例的）。
4. 测量噪声要讲方法学（N 次、worst-case 窗口），**单次采样不支撑统计结论**。
