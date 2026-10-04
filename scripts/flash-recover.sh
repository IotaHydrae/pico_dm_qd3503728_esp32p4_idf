#!/usr/bin/env bash
# 烧录一条总是能走通的路：先探哪个口活着，探不到就明确告诉你该按什么。
#
# 背景（实测细节见 S3 项目的 notes/flash-recovery.md；本仓这份脚本是继承来的，本板用不上）：
# PUD 应用用内部 USB-OTG
# （main/main.c: "USB OTG device on GPIO19/20"），而 ESP32-S3 的 USB-OTG 与
# USB-Serial-JTAG 共用同一对引脚 ⇒ 应用跑起来后 /dev/ttyACM* 会消失，
# 此时 esptool 的 --before usb_reset 也没用（它本身就走那个口）。
# 所以本脚本不做假设：探、报、按需等，而不是 sleep 一个猜出来的秒数。
#
# 用法:
#   scripts/flash-recover.sh --probe            # 只报状态，不烧
#   scripts/flash-recover.sh                    # 探到口就 idf.py flash
#   scripts/flash-recover.sh -- <idf.py 参数>   # 探到口就执行自定义命令
set -u

IDF_EXPORT=${IDF_EXPORT:-$HOME/esp/esp-idf/export.sh}
# 候选口：USB-Serial-JTAG 优先（ROM 下载态与不占用 USB 的应用都在它上面），
# 其后是 CH340 的 UART0。顺序即优先级，因为 ACM 这条已实测可用。
CANDIDATES=()
for p in /dev/ttyACM* /dev/ttyUSB*; do [ -e "$p" ] && CANDIDATES+=("$p"); done
PROBE_ONLY=0
CMD=()
while [ $# -gt 0 ]; do
	case "$1" in
	--probe) PROBE_ONLY=1 ;;
	--) shift; CMD=("$@"); break ;;
	*) CMD+=("$1") ;;
	esac
	shift
done

[ -f "$IDF_EXPORT" ] || { echo "找不到 IDF 环境: $IDF_EXPORT（用 IDF_EXPORT=... 指定）" >&2; exit 2; }
# shellcheck disable=SC1090
source "$IDF_EXPORT" >/dev/null 2>&1 || { echo "导出 IDF 环境失败: $IDF_EXPORT" >&2; exit 2; }
command -v esptool.py >/dev/null || { echo "esptool.py 不在 PATH（IDF 环境没导出成功？）" >&2; exit 2; }

# 就绪判据：esptool 能读到 MAC。单次 5 s 超时 ⇒ 探一个口最多 5 s，不盲等。
probe_port() {
	timeout 8 esptool.py -p "$1" --before default_reset chip_id 2>&1 | grep -q 'MAC:' && return 0
	return 1
}

echo "候选串口: ${CANDIDATES[*]:-（没有）}"
ALIVE=""
for p in "${CANDIDATES[@]:-}"; do
	[ -n "$p" ] || continue
	if probe_port "$p"; then ALIVE="$p"; echo "  $p 有响应（可烧录）"; break; fi
	echo "  $p 无响应"
done

# 应用占着 USB-OTG 时 ACM 会消失。应用里有一段 UART0 监听（main/boot_request.c），
# 收到 magic 就置 RTC force-download-boot 并重启 ⇒ USB-Serial-JTAG 回来。
# 这里反复发，直到某个口在"不复位"的前提下能应答（= 芯片确实进了下载态）。
send_boot_magic() {
	local tty
	for tty in /dev/ttyUSB*; do
		[ -e "$tty" ] || continue
		stty -F "$tty" 115200 raw -echo 2>/dev/null || continue
		printf 'PUD-BOOT\n' > "$tty" 2>/dev/null && echo "  已向 $tty 发送进入下载态请求"
	done
}

rom_ready() {   # 不复位就直接能应答 ⇒ 芯片已在 ROM 下载态
	timeout 8 esptool.py -p "$1" --before no_reset --after no_reset chip_id 2>&1 | grep -q 'MAC:'
}

if [ -z "$ALIVE" ]; then
	echo "没有口直接可用，尝试请应用自己进下载态（不按 BOOT）…" >&2
	deadline=$((SECONDS + 20))
	while [ $SECONDS -lt $deadline ]; do
		send_boot_magic
		for p in /dev/ttyACM*; do
			[ -e "$p" ] || continue
			if rom_ready "$p"; then ALIVE="$p"; break 2; fi
		done
	done
	[ -n "$ALIVE" ] && echo "  芯片已进入 ROM 下载态: $ALIVE"
fi

if [ -z "$ALIVE" ]; then
	cat >&2 <<'EOF'
没有可用的烧录口。最可能的原因：应用正在占用内部 USB-OTG，把 USB-Serial-JTAG 顶掉了。
按顺序试：

1) 手指进 ROM 下载态（本板唯一实测可靠的办法）：
   按住 BOOT 不放 → 按一下 RESET → 松开 BOOT。
   然后重跑本脚本：它会探到口再烧。
2) 物理 UART0（CH340，/dev/ttyUSB0）：本板**尚未验证成功**
   （实测 `No serial data received`）。要查的是 DTR/RTS 是否接到 EN/GPIO0；
   没接就得同样手动进下载态，并加 `--before no_reset`。
3) 长效办法：让应用自己支持"请求进入下载态"（方案 B 的细节见 S3 项目的 notes/flash-recovery.md；本仓这份是继承来的，本板用不上）。
EOF
	[ "$PROBE_ONLY" = 1 ] && exit 1
	# 轮询而不是 sleep：人按下 BOOT+RESET 后口会自己出现，出现就继续。
	echo "等待烧录口出现（Ctrl-C 取消）…" >&2
	deadline=$((SECONDS + 120))
	while [ $SECONDS -lt $deadline ]; do
		for p in /dev/ttyACM* /dev/ttyUSB*; do
			[ -e "$p" ] || continue
			if probe_port "$p"; then ALIVE="$p"; break 2; fi
		done
	done
	[ -n "$ALIVE" ] || { echo "等不到烧录口，放弃。" >&2; exit 1; }
	echo "出现: $ALIVE"
fi

[ "$PROBE_ONLY" = 1 ] && { echo "probe 模式：不烧录。"; exit 0; }
if [ ${#CMD[@]} -eq 0 ]; then CMD=(flash); fi
echo "执行: idf.py -p $ALIVE ${CMD[*]}"
# 不替调用者决定波特率：要改就自己传（例如 scripts/flash-recover.sh -- -b 460800 flash）
idf.py -p "$ALIVE" "${CMD[@]}"
