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

#include "main.h"
#include <math.h>

#define MACOS_SWITCH_MOVE_X 10
#define MACOS_SWITCH_MOVE_COUNT 5
#define ACCEL_POINTS 7

uint16_t get_jump_threshold(output_t *output, enum screen_pos_e direction) {
    const uint16_t NO_JUMP_THRESHOLD = 0;

    /* On a relative extension screen (index > 1) only the cross-PC portal edge
       is threshold-gated (P1: side display BOTTOM #4; P2: side display RIGHT
       #6) - every other edge is a physical wall or a local switch */
    if (output->screen_index > 1) {
        bool cross_portal =
            (global_state.layout_profile == LAYOUT_PROFILE_HOST &&
             output->number == OUTPUT_A && direction == BOTTOM) ||
            (global_state.layout_profile == LAYOUT_PROFILE_LAPTOP &&
             output->number == OUTPUT_B && direction == RIGHT);

        return cross_portal ? global_state.config.jump_threshold : NO_JUMP_THRESHOLD;
    }

    /* If on main screen but going away from the border, switch is local */
    if (output->pos == direction)
        return NO_JUMP_THRESHOLD;

    /* ... in all other cases, switch is non-local (jump to another pc) */
    return global_state.config.jump_threshold;
}

/* Check if our upcoming mouse movement would result in having to switch outputs */
enum screen_pos_e is_screen_switch_needed(output_t *output, int position, int offset) {
    enum screen_pos_e direction = (offset < 0) ? LEFT : RIGHT;

    /* No position offset implies no switch needed. */
    if (offset == 0)
        return NONE;

    /* Local switches (virtual desktop changes) have no gap, only cross-output jumps use threshold */
    uint16_t threshold = get_jump_threshold(output, direction);

    if (position + offset < MIN_SCREEN_COORD - threshold)
        return LEFT;

    if (position + offset > MAX_SCREEN_COORD + threshold)
        return RIGHT;

    return NONE;
}

/* Vertical-axis counterpart of is_screen_switch_needed. Returns TOP when the
   pointer would leave through the upper edge, BOTTOM through the lower edge.
   Threshold policy follows the vertical portals of the active layout profile
   (docs/screen-layout-proposal.md §8):
   - HOST:   A's lower edge (main display #2 / side display #4) and B's upper
             edge (#3) are cross-PC jumps -> jump_threshold
   - LAPTOP: A's lower edge is the cross-PC corner portal (#2), B's stacked
             screens switch internally with no gap (same as horizontal) */
enum screen_pos_e is_vertical_switch_needed(output_t *output, int position, int offset) {
    /* No position offset implies no switch needed. */
    if (offset == 0)
        return NONE;

    enum screen_pos_e direction = (offset < 0) ? TOP : BOTTOM;

    uint16_t threshold;

    if (global_state.layout_profile == LAYOUT_PROFILE_HOST)
        threshold = ((output->number == OUTPUT_A) == (direction == BOTTOM))
                        ? global_state.config.jump_threshold : 0;
    else
        threshold = (output->number == OUTPUT_A && direction == BOTTOM)
                        ? global_state.config.jump_threshold : 0;

    if (position + offset < MIN_SCREEN_COORD - threshold)
        return TOP;

    if (position + offset > MAX_SCREEN_COORD + threshold)
        return BOTTOM;

    return NONE;
}

/* Move mouse coordinate 'position' by 'offset', but don't fall off the screen */
int32_t move_and_keep_on_screen(int position, int offset) {
    /* Lowest we can go is 0 */
    if (position + offset < MIN_SCREEN_COORD)
        return MIN_SCREEN_COORD;

    /* Highest we can go is MAX_SCREEN_COORD */
    else if (position + offset > MAX_SCREEN_COORD)
        return MAX_SCREEN_COORD;

    /* We're still on screen, all good */
    return position + offset;
}

/* Implement basic mouse acceleration based on actual 2D movement magnitude.
   Returns the acceleration factor to apply to both x and y components. */
float calculate_mouse_acceleration_factor(int32_t offset_x, int32_t offset_y) {
    const struct curve {
        int value;
        float factor;
    } acceleration[ACCEL_POINTS] = {
                   // 4 |                                        *
        {2, 1},    //   |                                  *
        {5, 1.1},  // 3 |
        {15, 1.4}, //   |                       *
        {30, 1.9}, // 2 |                *
        {45, 2.6}, //   |        *
        {60, 3.4}, // 1 |  *
        {70, 4.0}, //    -------------------------------------------
    };             //        10    20    30    40    50    60    70

    if (offset_x == 0 && offset_y == 0)
        return 1.0;

    if (!global_state.config.enable_acceleration)
        return 1.0;

    // Calculate the 2D movement magnitude
    const float movement_magnitude = sqrtf((float)(offset_x * offset_x) + (float)(offset_y * offset_y));

    if (movement_magnitude <= acceleration[0].value)
        return acceleration[0].factor;

    if (movement_magnitude >= acceleration[ACCEL_POINTS-1].value)
        return acceleration[ACCEL_POINTS-1].factor;

    const struct curve *lower = NULL;
    const struct curve *upper = NULL;

    for (int i = 0; i < ACCEL_POINTS-1; i++) {
        if (movement_magnitude < acceleration[i + 1].value) {
            lower = &acceleration[i];
            upper = &acceleration[i + 1];
            break;
        }
    }

    // Should never happen, but just in case
    if (lower == NULL || upper == NULL)
        return 1.0;

    const float interpolation_pos = (movement_magnitude - lower->value) /
                                  (upper->value - lower->value);

    return lower->factor + interpolation_pos * (upper->factor - lower->factor);
}

/* Returns LEFT if need to jump left, RIGHT if right, NONE otherwise */
enum screen_pos_e update_mouse_position(device_t *state, mouse_values_t *values) {
    output_t *current    = &state->config.output[state->active_output];
    uint8_t reduce_speed = 0;

    /* Check if we are configured to move slowly */
    if (state->mouse_zoom)
        reduce_speed = MOUSE_ZOOM_SCALING_FACTOR;

    /* Calculate movement */
    float acceleration_factor = calculate_mouse_acceleration_factor(values->move_x, values->move_y);
    int offset_x = round(values->move_x * acceleration_factor * (current->speed_x >> reduce_speed));
    int offset_y = round(values->move_y * acceleration_factor * (current->speed_y >> reduce_speed));

    /* Determine if our upcoming movement would stay within the screen */
    enum screen_pos_e switch_direction = is_screen_switch_needed(current, state->pointer_x, offset_x);

    /* The horizontal axis takes precedence; only check Y if X stays on screen */
    if (switch_direction == NONE)
        switch_direction = is_vertical_switch_needed(current, state->pointer_y, offset_y);

    /* Update movement */
    state->pointer_x = move_and_keep_on_screen(state->pointer_x, offset_x);
    state->pointer_y = move_and_keep_on_screen(state->pointer_y, offset_y);

    /* Update buttons state */
    state->mouse_buttons = values->buttons;

    return switch_direction;
}

/* If we are active output, queue packet to mouse queue, else send them through UART */
void output_mouse_report(mouse_report_t *report, device_t *state) {
    if (CURRENT_BOARD_IS_ACTIVE_OUTPUT) {
        queue_mouse_report(report, state);
        state->last_activity[BOARD_ROLE] = time_us_64();
    } else {
        queue_packet((uint8_t *)report, MOUSE_REPORT_MSG, MOUSE_REPORT_LENGTH);
    }
}

/* Calculate and return Y coordinate when moving from screen out_from to screen out_to */
int16_t scale_y_coordinate(int screen_from, int screen_to, device_t *state) {
    output_t *from = &state->config.output[screen_from];
    output_t *to   = &state->config.output[screen_to];

    int size_to   = to->border.bottom - to->border.top;
    int size_from = from->border.bottom - from->border.top;

    /* If sizes match, there is nothing to do */
    if (size_from == size_to)
        return state->pointer_y;

    /* Moving from smaller ==> bigger screen
       y_a = top + (((bottom - top) * y_b) / HEIGHT) */

    if (size_from > size_to) {
        return to->border.top + ((size_to * state->pointer_y) / MAX_SCREEN_COORD);
    }

    /* Moving from bigger ==> smaller screen
       y_b = ((y_a - top) * HEIGHT) / (bottom - top) */

    if (state->pointer_y < from->border.top)
        return MIN_SCREEN_COORD;

    if (state->pointer_y > from->border.bottom)
        return MAX_SCREEN_COORD;

    return ((state->pointer_y - from->border.top) * MAX_SCREEN_COORD) / size_from;
}

void switch_to_another_pc(
    device_t *state, output_t *output, int output_to, int direction) {
    uint8_t *mouse_park_pos = &state->config.output[state->active_output].mouse_park_pos;

    int16_t mouse_y = (*mouse_park_pos == 0) ? MIN_SCREEN_COORD : /* Top */
                      (*mouse_park_pos == 1) ? MAX_SCREEN_COORD : /* Bottom */
                                               state->pointer_y;  /* Previous */

    mouse_report_t hidden_pointer = {.y = mouse_y, .x = MAX_SCREEN_COORD};

    output_mouse_report(&hidden_pointer, state);
    set_active_output(state, output_to);
    state->pointer_x = (direction == LEFT) ? MAX_SCREEN_COORD : MIN_SCREEN_COORD;
    state->pointer_y = scale_y_coordinate(output->number, 1 - output->number, state);

    /* v3: the target may have been left on its relative extension screen (#4
       in P1, #6 in P2), so reset screen index and coordinate mode explicitly */
    state->relative_mouse = false;
    state->config.output[output_to].screen_index = 1;
}

/* Side display physical pixel size, with fallback for zeroed (pre-v9) config */
static void side_display_px(uint16_t *width, uint16_t *height) {
    *width  = global_state.config.side_width_px;
    *height = global_state.config.side_height_px;

    if (*width == 0)
        *width = SIDE_DISPLAY_WIDTH_PX;
    if (*height == 0)
        *height = SIDE_DISPLAY_HEIGHT_PX;
}

/* One large-delta relative report sliding the OS cursor onto the side display.
   The relative HID interface has 16-bit axes, so a ~1920 px slide fits in a
   single report (millisecond-level). Buttons forced to 0 to avoid carrying
   press state across the switch, like the macOS workaround does. */
static void send_relative_pulse(device_t *state, int16_t dx, int16_t dy) {
    mouse_report_t pulse = {.x = dx, .y = dy, .mode = RELATIVE, .buttons = 0};
    output_mouse_report(&pulse, state);
}

/* #2 (both profiles): main display lower edge -> internal screen right edge.
   Corner mapping: the exit position along the main display's bottom edge
   becomes the entry height along the internal screen's right edge (equal
   proportion) - sweeping the bottom edge slides the cursor down the right
   edge, and the two screens' shared corner maps onto itself. */
void switch_main_bottom_to_laptop(device_t *state) {
    int16_t x0 = state->pointer_x;

    /* Pin the leaving cursor on the main display's bottom edge (absolute) */
    mouse_report_t park = {.x = x0, .y = MAX_SCREEN_COORD};
    output_mouse_report(&park, state);

    set_active_output(state, OUTPUT_B);
    state->relative_mouse = false;
    state->config.output[OUTPUT_B].screen_index = 1;

    state->pointer_x = MAX_SCREEN_COORD;
    state->pointer_y = x0;
}

/* #3 (P1): laptop internal screen upper edge -> side display bottom edge.
   The side display is relative, so the host cursor cannot be placed there
   directly (KB5003637 clamps absolute reports to the main display): park it
   absolutely on the main display's bottom-left corner, then send one
   relative pulse across the (bottom-aligned) arrangement boundary onto the
   side display's bottom edge at the kept X proportion. */
void switch_laptop_top_to_side(device_t *state) {
    int16_t x0 = state->pointer_x;
    uint16_t aux_w, aux_h;
    side_display_px(&aux_w, &aux_h);

    /* Pin the leaving cursor on the internal screen's upper edge */
    mouse_report_t park = {.x = x0, .y = MIN_SCREEN_COORD};
    output_mouse_report(&park, state);

    set_active_output(state, OUTPUT_A);

    /* Absolute park on the main display's bottom-left corner */
    mouse_report_t corner = {.x = MIN_SCREEN_COORD, .y = MAX_SCREEN_COORD};
    output_mouse_report(&corner, state);

    /* One relative pulse across into the side display's bottom edge:
       dx = x0/32767 * aux_w - (aux_w + 1), ranging from -1 (right edge)
       to -(aux_w + 1) (left edge) */
    send_relative_pulse(state,
                        (int16_t)((int32_t)x0 * aux_w / MAX_SCREEN_COORD - aux_w - 1),
                        -1);

    state->relative_mouse = true;
    state->config.output[OUTPUT_A].screen_index = 2;
    state->pointer_x = x0;
    state->pointer_y = MAX_SCREEN_COORD;
}

/* #4 (P1): side display lower edge -> internal screen upper edge. The leaving
   cursor is already pinned at the side display's bottom edge by Windows (the
   exit was triggered exactly there, and relative mode clamps it); absolute
   reports would be clamped to the main display, so no park report is sent. */
void switch_side_bottom_to_laptop(device_t *state) {
    int16_t x0 = state->pointer_x;

    set_active_output(state, OUTPUT_B);
    state->relative_mouse = false;
    state->config.output[OUTPUT_B].screen_index = 1;

    state->pointer_x = x0;
    state->pointer_y = MIN_SCREEN_COORD;
}

/* #5 (P2): main display left edge -> side display right edge. Park+pulse
   variant: the laptop's cursor is parked at the internal screen's top-right
   corner (absolute), then pulsed straight up along the shared right edge
   (right edges aligned in the Windows arrangement) onto the side display at
   the kept height. */
void switch_pc_to_side_horizontal(device_t *state) {
    int16_t y0 = state->pointer_y;
    uint16_t aux_w, aux_h;
    side_display_px(&aux_w, &aux_h);

    /* Pin the leaving cursor on the main display's left edge */
    mouse_report_t park = {.x = MIN_SCREEN_COORD, .y = y0};
    output_mouse_report(&park, state);

    set_active_output(state, OUTPUT_B);

    /* Absolute park on the internal screen's top-right corner */
    mouse_report_t corner = {.x = MAX_SCREEN_COORD, .y = MIN_SCREEN_COORD};
    output_mouse_report(&corner, state);

    /* One relative pulse up onto the side display's right edge at y0:
       dy = -(1 + (aux_h - 1) * (32767 - y0) / 32767), from -1 (bottom edge)
       to -aux_h (top edge) */
    send_relative_pulse(state, 0,
        -(int16_t)(1 + (int32_t)(aux_h - 1) * (MAX_SCREEN_COORD - y0) / MAX_SCREEN_COORD));

    state->relative_mouse = true;
    state->config.output[OUTPUT_B].screen_index = 2;
    state->pointer_x = MAX_SCREEN_COORD;
    state->pointer_y = y0;
}

/* #6 (P2): side display right edge -> main display left edge. Like #4, the
   leaving cursor stays pinned at the side display's right edge (relative
   mode, clamped by Windows), so no park report is sent. */
void switch_side_to_main_horizontal(device_t *state) {
    set_active_output(state, OUTPUT_A);
    state->relative_mouse = false;
    state->config.output[OUTPUT_A].screen_index = 1;

    state->pointer_x = MIN_SCREEN_COORD;
    state->pointer_y = scale_y_coordinate(OUTPUT_B, OUTPUT_A, state);
}

void switch_virtual_desktop_macos(device_t *state, int direction) {
    /*
     * Fix for MACOS: Before sending new absolute report setting X to 0:
     * 1. Move the cursor to the edge of the screen directly in the middle to handle screens
     *    of different heights
     * 2. Send relative mouse movement one or two pixels in the direction of movement to get
     *    the cursor onto the next screen
     */
    mouse_report_t edge_position = {
        .x = (direction == LEFT) ? MIN_SCREEN_COORD : MAX_SCREEN_COORD,
        .y = MAX_SCREEN_COORD / 2,
        .mode = ABSOLUTE,
        .buttons = state->mouse_buttons,
    };

    uint16_t move = (direction == LEFT) ? -MACOS_SWITCH_MOVE_X : MACOS_SWITCH_MOVE_X;
    mouse_report_t move_relative_one = {
        .x = move,
        .mode = RELATIVE,
        /* Force buttons to 0 for relative movement to avoid duplicating the button 
           press state, which would leave the relative HID mouse permanently stuck 
           down if the user is dragging an item while switching desktops. */
        .buttons = 0,
    };

    output_mouse_report(&edge_position, state);

    /* Once doesn't seem reliable enough, do it a few times */
    for (int i = 0; i < MACOS_SWITCH_MOVE_COUNT; i++)
        output_mouse_report(&move_relative_one, state);
}

void switch_virtual_desktop(device_t *state, output_t *output, int new_index, int direction) {
    switch (output->os) {
        case MACOS:
            switch_virtual_desktop_macos(state, direction);
            break;

        case WINDOWS:
            /* TODO: Switch to relative-only if index > 1, but keep tabs to switch back */
            state->relative_mouse = (new_index > 1);
            break;

        case LINUX:
        case ANDROID:
        case OTHER:
            /* Linux should treat all desktops as a single virtual screen, so you should leave
            screen_count at 1 and it should just work */
            break;
    }

    state->pointer_x       = (direction == RIGHT) ? MIN_SCREEN_COORD : MAX_SCREEN_COORD;
    output->screen_index = new_index;
}

/* Vertical counterpart of switch_virtual_desktop (internal screen change along
   the Y axis). The macOS workaround doesn't apply here; Windows switches to
   the relative HID interface the same way as horizontal desktop changes do. */
void switch_virtual_desktop_vertical(device_t *state, output_t *output, int new_index, int direction) {
    if (output->os == WINDOWS)
        state->relative_mouse = (new_index > 1);

    /* Enter the new screen through the physical edge we crossed */
    state->pointer_y     = (direction == BOTTOM) ? MIN_SCREEN_COORD : MAX_SCREEN_COORD;
    output->screen_index = new_index;
}

/*                               BORDER
                                   |
       .---------.    .---------.  |  .---------.    .---------.    .---------.
      ||    B2   ||  ||    B1   || | ||    A1   ||  ||    A2   ||  ||    A3   ||   (output, index)
      ||  extra  ||  ||   main  || | ||   main  ||  ||  extra  ||  ||  extra  ||   (main or extra)
       '---------'    '---------'  |  '---------'    '---------'    '---------'
          )___(          )___(     |     )___(          )___(          )___(

   v3 layout profiles (docs/screen-layout-proposal.md §8): the side display is
   index 2 (relative) on whichever output owns it.
   - HOST   {A:2, B:1}, A.pos = LEFT:  [A2 side][A1 main][BORDER][B1]
     horizontal cross-PC portal = B1 right edge (#1); A main display's right
     edge is a wall; A main display's left edge <-> side display = internal.
   - LAPTOP {A:1, B:2}, A.pos = RIGHT: [B2 side][B1 main][BORDER][A1]
     horizontal cross-PC portals = B1 right edge (#1), B2 right edge (#6) and
     A1 left edge (#5); B's horizontal internal edges are walls (stacked). */
void do_screen_switch(device_t *state, int direction) {
    output_t *output = &state->config.output[state->active_output];

    /* No switching allowed if explicitly disabled or in gaming mode */
    if (state->switch_lock || state->gaming_mode)
        return;

    /* We want to jump in the direction of the other computer */
    if (output->pos != direction) {
        if (output->screen_index == 1) { /* We are at the border -> switch outputs */
            /* No switching allowed if mouse button is held. Should only apply to the border! */
            if (state->mouse_buttons)
                return;

            /* HOST profile: A's main display right edge is a physical dead
               end (the side display hangs off its LEFT edge); only B's
               internal screen right edge crosses PCs (#1) */
            if (state->layout_profile == LAYOUT_PROFILE_HOST && output->number == OUTPUT_A)
                return;

            /* LAPTOP profile: leaving the main display through its left edge
               enters the side display via park+pulse (#5) */
            if (state->layout_profile == LAYOUT_PROFILE_LAPTOP && output->number == OUTPUT_A) {
                switch_pc_to_side_horizontal(state);
                return;
            }

            switch_to_another_pc(state, output, 1 - state->active_output, direction);
        }
        /* Not on the main screen, moving toward the border. In the LAPTOP
           profile output B's screens are stacked vertically, so its horizontal
           edges are physically non-existent - except the side display's right
           edge, which is the cross-PC portal to the main display (#6) */
        else if (state->layout_profile == LAYOUT_PROFILE_LAPTOP && output->number == OUTPUT_B) {
            if (direction == RIGHT) {
                if (state->mouse_buttons)
                    return;
                switch_side_to_main_horizontal(state);
            }
            return;
        }
        else
            switch_virtual_desktop(state, output, output->screen_index - 1, direction);
    }

    /* We want to jump away from the other computer, only possible if there is another screen to jump to */
    else if (output->screen_index < output->screen_count) {
        /* Same LAPTOP-profile guard: no physical horizontal edge between
           output B's stacked screens */
        if (state->layout_profile == LAYOUT_PROFILE_LAPTOP && output->number == OUTPUT_B)
            return;

        /* HOST profile: main display's left edge <-> side display right edge,
           an internal switch on the physically adjacent pair (#5/#6 of P1) */
        switch_virtual_desktop(state, output, output->screen_index + 1, direction);
    }
}

/* Vertical-axis screen switching, driven by the layout profile (v3):
   - HOST:   side display on A. Vertical portals: A1 lower edge -> laptop
             (corner map, #2), A2 (side display) lower edge -> laptop (#4),
             B1 upper edge -> side display (park+pulse, #3)
   - LAPTOP: side display on B. Vertical portals: A1 lower edge -> laptop
             (corner map, #2, cross-PC); B's stacked screens switch internally
             (side display above the internal screen, natural handoff) */
void do_vertical_switch(device_t *state, int direction) {
    output_t *output = &state->config.output[state->active_output];

    /* No switching allowed if explicitly disabled or in gaming mode */
    if (state->switch_lock || state->gaming_mode)
        return;

    if (state->layout_profile == LAYOUT_PROFILE_HOST) {
        if (output->number == OUTPUT_B) {
            /* B's internal screen upper edge -> side display bottom edge (#3) */
            if (direction == TOP && output->screen_index == 1) {
                if (state->mouse_buttons)
                    return;
                switch_laptop_top_to_side(state);
            }
            /* B lower edge: physical wall */
        } else {
            /* Output A: lower edge crosses to the laptop from both of its
               screens (main display #2, side display #4) */
            if (direction == BOTTOM) {
                if (state->mouse_buttons)
                    return;

                if (output->screen_index == 1)
                    switch_main_bottom_to_laptop(state);
                else
                    switch_side_bottom_to_laptop(state);
            }
            /* A upper edge: physical wall */
        }
    }

    else {
        if (output->number == OUTPUT_A) {
            /* A's main display lower edge -> laptop internal screen right
               edge (#2, corner mapping, cross-PC) */
            if (direction == BOTTOM && output->screen_index == 1) {
                if (state->mouse_buttons)
                    return;
                switch_main_bottom_to_laptop(state);
            }
            /* A upper edge: physical wall */
        } else {
            /* B's screens are stacked (side display above the internal
               screen); their shared edges are internal switches, no gap */
            if (direction == TOP && output->screen_index < output->screen_count)
                switch_virtual_desktop_vertical(state, output, output->screen_index + 1, direction);

            else if (direction == BOTTOM && output->screen_index > 1)
                switch_virtual_desktop_vertical(state, output, output->screen_index - 1, direction);
        }
    }
}

static inline bool extract_value(bool uses_id, int32_t *dst, report_val_t *src, uint8_t *raw_report, int len) {
    /* The report must contain at least one payload byte. */
    if (len <= uses_id)
        return false;

    /* If the report uses an ID, the first byte must match the expected report ID */
    if (uses_id && *raw_report != src->report_id)
        return false;

    /* Extract the value, adjust pointer and length if report ID is used */
    *dst = get_report_value(raw_report + uses_id, len - uses_id, src);
    return true;
}

void extract_report_values(uint8_t *raw_report, int len, device_t *state, mouse_values_t *values, hid_interface_t *iface) {
    /* Interpret values depending on the current protocol used. */
    if (iface->protocol == HID_PROTOCOL_BOOT) {
        hid_mouse_report_t *mouse_report = (hid_mouse_report_t *)raw_report;

        values->move_x  = mouse_report->x;
        values->move_y  = mouse_report->y;
        values->wheel   = mouse_report->wheel;
        values->pan     = mouse_report->pan;
        values->buttons = mouse_report->buttons;
        return;
    }
    mouse_t *mouse = &iface->mouse;
    bool uses_id = iface->uses_report_id;

    extract_value(uses_id, &values->move_x, &mouse->move_x, raw_report, len);
    extract_value(uses_id, &values->move_y, &mouse->move_y, raw_report, len);
    extract_value(uses_id, &values->wheel, &mouse->wheel, raw_report, len);
    extract_value(uses_id, &values->pan, &mouse->pan, raw_report, len);

    if (!extract_value(uses_id, &values->buttons, &mouse->buttons, raw_report, len)) {
        values->buttons = state->mouse_buttons;
    }
}

mouse_report_t create_mouse_report(device_t *state, mouse_values_t *values) {
    mouse_report_t mouse_report = {
        .buttons = values->buttons,
        .x       = state->pointer_x,
        .y       = state->pointer_y,
        .wheel   = values->wheel,
        .pan     = values->pan,
        .mode    = ABSOLUTE,
    };

    /* Workaround for Windows multiple desktops */
    if (state->relative_mouse || state->gaming_mode) {
        mouse_report.x = values->move_x;
        mouse_report.y = values->move_y;
        mouse_report.mode = RELATIVE;
    }

    return mouse_report;
}

void process_mouse_report(uint8_t *raw_report, int len, uint8_t itf, hid_interface_t *iface) {
    mouse_values_t values = {0};
    device_t *state = &global_state;

    /* Interpret the mouse HID report, extract and save values we need. */
    extract_report_values(raw_report, len, state, &values, iface);

    /* If nothing changed, don't send a report. This prevents composite keyboards
       (e.g. QMK) that expose a mouse HID interface from generating spurious
       absolute position reports when they send zero-movement mouse reports during
       keyboard events. */
    if (values.move_x == 0 && values.move_y == 0 &&
        values.wheel == 0 && values.pan == 0 &&
        values.buttons == state->mouse_buttons) {
        return;
    }

    /* Calculate and update mouse pointer movement. */
    enum screen_pos_e switch_direction = update_mouse_position(state, &values);

    /* Create the report for the output PC based on the updated values */
    mouse_report_t report = create_mouse_report(state, &values);

    /* Move the mouse, depending where the output is supposed to go */
    output_mouse_report(&report, state);

    /* We use the mouse to switch outputs: LEFT/RIGHT on the horizontal axis,
       TOP/BOTTOM through the vertical portal of the active layout profile */
    if (switch_direction != NONE) {
        if (switch_direction == LEFT || switch_direction == RIGHT)
            do_screen_switch(state, switch_direction);
        else
            do_vertical_switch(state, switch_direction);
    }
}

/* ==================================================== *
 * Mouse Queue Section
 * ==================================================== */

void process_mouse_queue_task(device_t *state) {
    mouse_report_t report = {0};

    /* We need to be connected to the host to send messages */
    if (!state->tud_connected)
        return;

    /* Peek first, if there is anything there... */
    if (!queue_try_peek(&state->mouse_queue, &report))
        return;

    /* If we are suspended, let's wake the host up */
    if (tud_suspended())
        tud_remote_wakeup();

    /* If it's not ready, we'll try on the next pass */
    if (!tud_hid_n_ready(ITF_NUM_HID))
        return;

    /* If the interface is configured as a keyboard in boot protocol, discard mouse data. */
    if (report.mode == ABSOLUTE && tud_hid_n_get_protocol(ITF_NUM_HID) == HID_PROTOCOL_BOOT) {
        queue_try_remove(&state->mouse_queue, &report);
        return;
    }

    /* Try sending it to the host, if it's successful */
    bool succeeded
        = tud_mouse_report(report.mode, report.buttons, report.x, report.y, report.wheel, report.pan);

    /* ... then we can remove it from the queue */
    if (succeeded)
        queue_try_remove(&state->mouse_queue, &report);
}

void queue_mouse_report(mouse_report_t *report, device_t *state) {
    /* It wouldn't be fun to queue up a bunch of messages and then dump them all on host */
    if (!state->tud_connected)
        return;

    queue_try_add(&state->mouse_queue, report);
}
