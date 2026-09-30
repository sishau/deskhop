/*
 * This file is part of DeskHop (https://github.com/hrvach/deskhop).
 * Copyright (c) 2025 Hrvoje Cavrak
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, version 3.
 *
 * See the file LICENSE for the full license text.
 */

#ifndef TEXT_CHANNEL_H_
#define TEXT_CHANNEL_H_

#include <stdbool.h>
#include <stdint.h>

#include "packet.h"

/* Maximum text size the channel buffers on a board, in bytes.
   UTF-8 Chinese text costs 3 bytes per character => ~2.7k chars. */
#define TC_BUF_SIZE 8192

/* Bytes of text carried per packet: data[1..7] on the push path,
   data[0..6] on the pull path. */
#define TC_CHUNK_PAYLOAD 7

/* USB vendor report types (uart_packet_t.type on the SET_REPORT path).
   These travel only over USB; on the UART they are relayed as
   TEXT_BEGIN_MSG / TEXT_DATA_MSG. */
enum tc_usb_msg_e {
    TC_USB_PUSH_BEGIN = 1, /* data[0..1] = total length, little endian    */
    TC_USB_PUSH_DATA  = 2, /* data[0] = seq, data[1..7] = 7 bytes of text */
    TC_USB_PULL_REQ   = 3, /* Restart the pull side, data ignored         */
};

/* Types used in GET_REPORT responses (same 12-byte packet layout). */
#define TC_USB_PULL_DATA  0x05 /* data[0..6] = text, data[7] = EOF|len flag */
#define TC_USB_PULL_EMPTY 0xFF /* Nothing buffered on the pull side         */

/* UART packet from the other board (registered in uart_handler[]) */
void handle_text_channel_msg(uart_packet_t *pkt, device_t *state);

/* USB SET_REPORT chunk from the local host (normal mode vendor interface) */
void tc_handle_usb_push(uart_packet_t *pkt);

/* USB GET_REPORT answer feeding the local host (normal mode vendor interface).
   Returns the number of bytes written to buffer, 0 to stall the request. */
uint16_t tc_get_report(uint8_t *buffer, uint16_t len);

#endif /* TEXT_CHANNEL_H_ */
