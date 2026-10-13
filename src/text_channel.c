/*
 * This file is part of DeskHop (https://github.com/hrvach/deskhop).
 * Copyright (c) 2025 Hrvoje Cavrak
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, version 3.
 *
 * See the file LICENSE for the full license text.
 *
 * ---- Text channel -----------------------------------------------------------
 * Transfers a single block of plain text (<= TC_BUF_SIZE bytes) between the
 * two boards over the vendor HID interface in normal mode.
 *
 * Push path:  host --SET_REPORT--> local board --UART--> other board --buf
 * Pull path:  host <--GET_REPORT-- other board <== (burn after reading)
 *
 * The sending board only relays; only the receiving board buffers text.
 * The buffer is single-shot: a new push supersedes text that was not pulled
 * yet, and a fully read buffer is wiped (burn after reading).
 */

#include "main.h"
#include "text_channel.h"

/* ------------------------------ State ------------------------------------ */

typedef struct {
    uint16_t total;    /* Text length in bytes, as announced by BEGIN     */
    uint16_t written;  /* Bytes written to the buffer so far              */
    uint8_t next_seq; /* Sequence number the next DATA packet must carry */
    bool active;      /* True between BEGIN and the final DATA packet    */
} tc_rx_state_t;

typedef struct {
    uint16_t len;    /* Length of the buffered text, 0 = nothing to read */
    uint16_t offset; /* Read position of the pull side                   */
} tc_tx_state_t;

static tc_rx_state_t tc_rx;
static tc_tx_state_t tc_tx;
static uint8_t tc_buf[TC_BUF_SIZE];

/* Diagnostics: how many TC_USB_LAYOUT_SELECT packets actually reached the
   handler. A host tool reads this back via GET_REPORT (PULL_EMPTY response,
   bytes 4-6: profile / count / active_output) before and after sending, so a
   stalled count proves the packet never arrived and locates the fault at the
   USB layer. */
static uint8_t tc_type4_count;

/* --------------------------- Receiving side ------------------------------ */

/* Set up a fresh transfer session. Supersedes any previous one. */
static void tc_rx_begin(uint16_t total) {
    if (total == 0 || total > TC_BUF_SIZE)
        return;

    tc_rx.total = total;
    tc_rx.written = 0;
    tc_rx.next_seq = 0;
    tc_rx.active = true;
}

/* Store one chunk. A sequence gap aborts the whole session. */
static void tc_rx_data(const uint8_t *data) {
    if (!tc_rx.active || data[0] != tc_rx.next_seq) {
        tc_rx.active = false;
        return;
    }

    uint16_t remain = tc_rx.total - tc_rx.written;
    uint16_t chunk = (TC_CHUNK_PAYLOAD < remain) ? TC_CHUNK_PAYLOAD : remain;

    memcpy(&tc_buf[tc_rx.written], &data[1], chunk);
    tc_rx.written += chunk;
    tc_rx.next_seq++;

    if (tc_rx.written != tc_rx.total)
        return;

    /* Transfer complete - publish it to the pull side. */
    tc_rx.active = false;
    tc_tx.len = tc_rx.total;
    tc_tx.offset = 0;
}

/* UART packets relayed by the other board. */
void handle_text_channel_msg(uart_packet_t *pkt, device_t *state) {
    /* Activity indicator: blink while a transfer is in flight so the user
       can tell when it is done (blinks stop shortly after the last packet). */
    blink_led_onboard(state);

    switch (pkt->type) {
        case TEXT_BEGIN_MSG:
            tc_rx_begin(pkt->data[0] | (pkt->data[1] << 8));
            break;

        case TEXT_DATA_MSG:
            tc_rx_data(pkt->data);
            break;

        default:
            break;
    }
}

/* --------------------------- Sending side -------------------------------- */

/* USB SET_REPORT chunks from the local host. The sending board never buffers
   text itself, it just relays it to the other board over the UART. */
void tc_handle_usb_push(uart_packet_t *pkt) {
    /* Activity indicator, same rationale as the receiving side. */
    blink_led_onboard(&global_state);

    switch (pkt->type) {
        case TC_USB_PUSH_BEGIN:
            queue_packet(pkt->data, TEXT_BEGIN_MSG, PACKET_DATA_LENGTH);
            break;

        case TC_USB_PUSH_DATA:
            queue_packet(pkt->data, TEXT_DATA_MSG, PACKET_DATA_LENGTH);
            break;

        case TC_USB_PULL_REQ:
            /* Allow a host to restart an interrupted read from the top. */
            tc_tx.offset = 0;
            break;

        case TC_USB_LAYOUT_SELECT: {
            /* The KVM-state watcher in the resident host tool reports the
               side display's ownership via monitor count. Only act on an
               actual change so manual hotkeys and repeats stay harmless. */
            uint8_t profile = pkt->data[0];
            tc_type4_count++;

            if (profile > LAYOUT_PROFILE_LAPTOP)
                break;

            if (global_state.layout_profile != profile) {
                layout_profile_select(&global_state, profile);
            } else {
                /* Local side already matches, but the peer may still be stale
                   if it missed the original sync (UART TX queue drop). Send a
                   re-announcement so a manual/watched resend can repair it;
                   the receiving board applies idempotently and blinks. */
                global_state.layout_resend_profile = profile;
                global_state.layout_resend_left    = 4;
                global_state.layout_resend_at      = 0;
                send_value(profile, LAYOUT_SELECT_MSG);
            }
            break;
        }

        default:
            break;
    }
}

/* USB GET_REPORT answers for the local host. */
uint16_t tc_get_report(uint8_t *buffer, uint16_t len) {
    if (len < RAW_PACKET_LENGTH)
        return 0;

    uart_packet_t pkt = {.type = TC_USB_PULL_DATA};

    if (tc_tx.len == 0) {
        /* Nothing buffered - tell the host there is nothing to pull, and
           piggyback board diagnostics on the (otherwise all-zero) data:
           [0] layout_profile, [1] type=4 packets seen, [2] active_output,
           [3] bit0 = config mode active, bits1.. = BOARD_ROLE. */
        pkt.type = TC_USB_PULL_EMPTY;
        pkt.data[0] = global_state.layout_profile;
        pkt.data[1] = tc_type4_count;
        pkt.data[2] = global_state.active_output;
        pkt.data[3] = (global_state.config_mode_active ? 1 : 0) | (BOARD_ROLE << 1);
    } else {
        /* Activity indicator while the pull side drains the buffer. */
        blink_led_onboard(&global_state);

        uint16_t remain = tc_tx.len - tc_tx.offset;
        uint16_t chunk = (TC_CHUNK_PAYLOAD < remain) ? TC_CHUNK_PAYLOAD : remain;

        memcpy(&pkt.data[0], &tc_buf[tc_tx.offset], chunk);
        tc_tx.offset += chunk;

        /* data[7] flags: 0 = a full 7-byte chunk with more to come,
           0x80|n (n = 1..7) = the final chunk, carrying n bytes of text. */
        pkt.data[7] = (chunk == remain) ? (0x80 | chunk) : 0x00;
    }

    /* Same framing as the UART packets, so one parser serves both sides. */
    write_raw_packet(buffer, &pkt);

    /* Burn after reading - wipe the text once it is fully consumed. */
    if (tc_tx.len != 0 && tc_tx.offset == tc_tx.len) {
        memset(tc_buf, 0, tc_tx.len);
        tc_tx.len = 0;
    }

    return RAW_PACKET_LENGTH;
}
