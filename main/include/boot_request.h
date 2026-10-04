#pragma once
/* 见 boot_request.c：监听 UART0 上的 magic，收到就让芯片进 ROM 下载态。 */
void pud_boot_request_start(void);
