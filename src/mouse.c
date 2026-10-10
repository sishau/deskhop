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

#define ACCEL_POINTS 7

/* Single-report movement cap, in 0~32767 strip units (~1/8 screen).
   High-DPI mice plus acceleration can emit deltas larger than the remaining
   distance to an edge; without a cap the cursor teleports across it in one
   report, which feels like a premature screen switch ("halfway switch"). */
#define OFFSET_CAP 4096
/* After an ABSOLUTE report is submitted, hold RELATIVE ones this many queue
   task ticks (2 kHz -> 5 ms = 5 full-speed USB frames) so the host always
   consumes the park before its pulse, regardless of endpoint poll order. */
#define REL_HOLD_TICKS 10

/* Pulse landings aim this many physical pixels short of the shared edge.
   Windows keeps the side display's arrangement position per monitor
   identity, and the KVM box's faked EDID can re-enumerate with a few
   pixels of vertical offset: a landing exactly ON the shared edge then
   falls outside the side display, and the OS clamps the cursor back onto
   the primary - the "pulse did nothing" symptom. Aiming short of the edge
   makes the landing immune to +-PULSE_EDGE_MARGIN_PX of arrangement slop. */
#define PULSE_EDGE_MARGIN_PX 7

/* P2 side-display entry robustness. The park sits this many px left of the
   shared corner so the upward crossing happens through solidly overlapping
   columns instead of the single boundary pixel column, and the landing keeps
   a buffer from the re-entry edge. The pulse itself is split into a burst of
   small reports: the target machine's input stack has been measured to pass
   streaming deltas faithfully while a single huge delta did not survive it
   (k=1000 ledger tracking perfect, single -540-count pulse landing ~8px). */
#define PARK_X_MARGIN_PX   64
#define PULSE_CHUNK_COUNTS 96

/* Queue-task ticks (2 kHz) between burst chunks. Drained flat-out the burst
   runs at ~96 counts per USB frame (~96k counts/s) - far beyond any human
   flick and inside the regime the laptop's input stack distorts (a single
   -540-count delta was eaten to ~8px; streaming-speed deltas are faithful).
   96 counts / 4 ms = 24k counts/s, the proven-faithful regime; a full-height
   pulse still completes in ~45 ms, imperceptible. */
#define PULSE_CHUNK_PERIOD 8

/* Fractional remainders of the exact virtual-coordinate accumulation on the
   relative side display (see detect_and_update). Bounded by +-side_px. */
static int32_t rel_rem_x = 0, rel_rem_y = 0;

/* Side display physical pixel size, with fallback for zeroed (pre-v9) config */
static void side_display_px(uint16_t *width, uint16_t *height) {
    *width  = global_state.config.side_width_px;
    *height = global_state.config.side_height_px;

    if (*width == 0)
        *width = SIDE_DISPLAY_WIDTH_PX;
    if (*height == 0)
        *height = SIDE_DISPLAY_HEIGHT_PX;
}

/* ====================== v4: one signed coordinate plane ======================
   Origin = main display top-left, y grows downward (HID convention):

         side display     |  main display           y ∈ [0, MAX]
         internal screen  |  virtual corridor       y ∈ [MAX+1, 2MAX+1]
                          x = 0
      x ∈ [−MAX, −1]        x ∈ [0, MAX]

   - main display / internal screen are the two Windows primaries: driven
     with absolute reports, 1:1 mapping, zero drift, no calibration.
   - side display is relative-only (KB5003637 clamps absolute reports to the
     primary): driven with raw deltas, tracked exactly in plane units.
   - virtual corridor hides the cursor between the two primaries: the
     diagonal internal <-> main-display path walks through it and both ends
     land proportionally and exactly (absolute on both sides).

   Crossing an axis switches cells. Edge types by layout profile:
   - natural handoff (same machine, no gap):  P1 main<->side, P2 side<->internal
   - direct cross-PC exit (target is a primary: exact absolute landing)
   - pulse entry (cross-PC into the side display: park on the local primary,
     one relative pulse sized in px and converted to counts via the target
     machine's calibrated pointer scale k - exact for any constant k)
   - corridor (crossing hides the cursor, exit reappears proportionally)     */

typedef enum {
    CELL_MAIN,      /* x >= 0, y <= MAX: host main display (absolute)  */
    CELL_SIDE,      /* x <  0, y <= MAX: side display (relative)       */
    CELL_INTERNAL,  /* x <  0, y >  MAX: laptop internal (absolute)    */
    CELL_VIRTUAL,   /* x >= 0, y >  MAX: corridor, no output           */
} plane_cell_e;

#define BOTTOM_ROW_MIN (MAX_SCREEN_COORD + 1)
#define PLANE_Y_MAX    (2 * MAX_SCREEN_COORD + 1)

typedef struct {
    bool    cross;
    bool    horizontal;
    int     direction;   /* enum screen_pos_e: LEFT/RIGHT or TOP/BOTTOM */
    int32_t other;       /* non-crossing coordinate at the crossing point */
} crossing_t;

static plane_cell_e cell_of(int32_t x, int32_t y) {
    if (y <= MAX_SCREEN_COORD)
        return (x >= 0) ? CELL_MAIN : CELL_SIDE;
    return (x >= 0) ? CELL_VIRTUAL : CELL_INTERNAL;
}

static int32_t clamp_i32(int32_t v, int32_t lo, int32_t hi) {
    return (v < lo) ? lo : (v > hi) ? hi : v;
}

/* Keep the plane inside the current cell (outer borders are walls). */
static void cell_clamp(plane_cell_e cell, int32_t *x, int32_t *y) {
    int32_t xlo = 0, xhi = MAX_SCREEN_COORD, ylo = 0, yhi = MAX_SCREEN_COORD;

    if (cell == CELL_SIDE || cell == CELL_INTERNAL) {
        xlo = -MAX_SCREEN_COORD;
        xhi = -1;
    }
    if (cell == CELL_INTERNAL || cell == CELL_VIRTUAL) {
        ylo = BOTTOM_ROW_MIN;
        yhi = PLANE_Y_MAX;
    }
    *x = clamp_i32(*x, xlo, xhi);
    *y = clamp_i32(*y, ylo, yhi);
}

/* The plane is the single source of truth; pointer_x/y mirror the current
   cell's screen-local absolute coordinates (what the reports carry). */
static void set_plane_position(device_t *state, int32_t px, int32_t py) {
    state->plane_x = px;
    state->plane_y = py;

    switch (cell_of(px, py)) {
    case CELL_INTERNAL:
        state->pointer_x = px + MAX_SCREEN_COORD;
        state->pointer_y = py - BOTTOM_ROW_MIN;
        break;
    case CELL_SIDE:
        state->pointer_x = px + MAX_SCREEN_COORD;
        state->pointer_y = py;
        break;
    default:
        state->pointer_x = px;
        state->pointer_y = py;
        break;
    }
}

/* Crossing policy per (cell, direction): does this edge lead anywhere, and
   is it a same-machine handoff (no gap) or a threshold-gated jump? */
static bool edge_policy(plane_cell_e cell, int direction, bool *natural) {
    bool host = (global_state.layout_profile == LAYOUT_PROFILE_HOST);
    *natural = false;

    switch (cell) {
    case CELL_MAIN:
        if (direction == LEFT)   { *natural = host; return true; }  /* side next door  */
        if (direction == BOTTOM) return true;                       /* corridor        */
        return false;                                               /* RIGHT/UP walls  */
    case CELL_SIDE:
        if (direction == RIGHT)  { *natural = host; return true; }  /* back to main    */
        if (direction == BOTTOM) { *natural = !host; return true; } /* P2: internal    */
        return false;                                               /* LEFT/UP walls   */
    case CELL_INTERNAL:
        if (direction == TOP)    { *natural = !host; return true; } /* P2: side above  */
        if (direction == RIGHT)  return true;                       /* corridor        */
        return false;                                               /* LEFT/DOWN walls */
    case CELL_VIRTUAL:
        return (direction == TOP || direction == LEFT);             /* corridor exits  */
    }
    return false;
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

/* Pointer-scale calibration (permille, 1000 = 1 count = 1 px) of the machine
   that RENDERS the side display's reports - its owner in the active profile.
   The math runs on the mouse board, but the OS applying the scale is the
   owner's, so cross-profile paths need the peer's value (via heartbeat). */
static uint16_t side_k_pm(device_t *state) {
    /* k describes the machine that renders the side display (its OS pointer
       scale). Both boards carry both outputs' k in their mirrored config,
       so the pulse sender looks up the target machine's k locally - no
       inter-board sync needed. */
    uint8_t owner = (state->layout_profile == LAYOUT_PROFILE_HOST) ? OUTPUT_A : OUTPUT_B;
    uint16_t k = state->config.output[owner].pointer_k_pm;
    /* 0 = unset (default); anything outside the sane calibration range is
       treated as 1:1 rather than blowing up the counts conversion. */
    if (k < 200 || k > 5000)
        k = 1000;
    return k;
}

/* Scan one axis for a boundary crossing. Returns the fraction t ∈ (0,1] of
   the offset at which the (threshold) boundary is reached, or 0 = no
   crossing (wall, not reached, or blocked by a held mouse button). */
static float axis_scan(device_t *state, plane_cell_e cell, bool horizontal,
                       int32_t offset, int *direction) {
    bool natural = false;
    if (offset == 0)
        return 0.0f;

    int32_t need, reach;

    if (horizontal) {
        *direction = (offset < 0) ? LEFT : RIGHT;
        if (!edge_policy(cell, *direction, &natural))
            return 0.0f;
        if (!natural && state->mouse_buttons)
            return 0.0f;   /* no cross-PC jumps while dragging */

        /* Boundary x = 0: left column ends at -1, right column starts at 0.
           Natural handoffs sit on a physical edge the OS cursor crosses
           immediately, so they fire at the exact border - a threshold gap
           here would pin the cursor on the leaving edge (absolute reports
           commanding it back) and mismatch the anchor by threshold/scale.
           Only gated cross-PC edges keep the jump_threshold margin. */
        need = (*direction == LEFT) ? -1 : 0;
        if (!natural) {
            need += (*direction == LEFT)
                ? -(int32_t)global_state.config.jump_threshold
                : (int32_t)global_state.config.jump_threshold;
        }
        reach = state->plane_x + offset;
        if ((*direction == LEFT) ? (reach > need) : (reach < need))
            return 0.0f;
    } else {
        *direction = (offset < 0) ? TOP : BOTTOM;
        if (!edge_policy(cell, *direction, &natural))
            return 0.0f;
        if (!natural && state->mouse_buttons)
            return 0.0f;

        /* Boundary y = MAX / MAX+1 between the two rows; same natural/gated
           rule as the horizontal boundary above. */
        need = (*direction == BOTTOM) ? BOTTOM_ROW_MIN : MAX_SCREEN_COORD;
        if (!natural) {
            need += (*direction == BOTTOM)
                ? (int32_t)global_state.config.jump_threshold
                : -(int32_t)global_state.config.jump_threshold;
        }
        reach = state->plane_y + offset;
        if ((*direction == BOTTOM) ? (reach < need) : (reach > need))
            return 0.0f;
    }

    return (float)(need - (horizontal ? state->plane_x : state->plane_y)) /
           (float)offset;
}

/* Clamp the non-crossing coordinate into the target cell (horizontal
   crossings stay in the same row, vertical ones in the same column). */
static int32_t clamp_other(int32_t v, plane_cell_e cell, bool is_y) {
    bool bottom_row = (cell == CELL_INTERNAL || cell == CELL_VIRTUAL);
    bool left_col   = (cell == CELL_SIDE     || cell == CELL_INTERNAL);

    if (is_y)
        return bottom_row ? clamp_i32(v, BOTTOM_ROW_MIN, PLANE_Y_MAX)
                          : clamp_i32(v, 0, MAX_SCREEN_COORD);
    return left_col ? clamp_i32(v, -MAX_SCREEN_COORD, -1)
                    : clamp_i32(v, 0, MAX_SCREEN_COORD);
}

/* Compute per-report movement, detect plane crossings, advance the plane.
   The caller emits the movement report first (on exits it doubles as the
   park report at the leaving edge), then handles the crossing. */
static crossing_t detect_and_update(device_t *state, mouse_values_t *values) {
    plane_cell_e cell = cell_of(state->plane_x, state->plane_y);
    output_t *current = &state->config.output[state->active_output];
    uint8_t reduce_speed = state->mouse_zoom ? MOUSE_ZOOM_SCALING_FACTOR : 0;
    crossing_t c = {false, false, NONE, 0};
    int32_t ox, oy;

    if (cell == CELL_SIDE) {
        /* Relative side display: the OS cursor moves by the RAW deltas scaled
           by the pointer multiplier k (EPP off = constant), so the plane
           accumulates move * k * 32767 / side_px exactly, with remainder. */
        uint16_t aux_w, aux_h;
        side_display_px(&aux_w, &aux_h);
        uint16_t k = side_k_pm(state);

        int64_t num = (int64_t)values->move_x * k * MAX_SCREEN_COORD + rel_rem_x;
        ox          = (int32_t)(num / ((int64_t)aux_w * 1000));
        rel_rem_x   = (int32_t)(num % ((int64_t)aux_w * 1000));

        num        = (int64_t)values->move_y * k * MAX_SCREEN_COORD + rel_rem_y;
        oy         = (int32_t)(num / ((int64_t)aux_h * 1000));
        rel_rem_y  = (int32_t)(num % ((int64_t)aux_h * 1000));
    } else {
        float acceleration_factor =
            calculate_mouse_acceleration_factor(values->move_x, values->move_y);
        ox = round(values->move_x * acceleration_factor * (current->speed_x >> reduce_speed));
        oy = round(values->move_y * acceleration_factor * (current->speed_y >> reduce_speed));
    }

    ox = clamp_i32(ox, -OFFSET_CAP, OFFSET_CAP);
    oy = clamp_i32(oy, -OFFSET_CAP, OFFSET_CAP);

    state->mouse_buttons = values->buttons;

    if (!state->switch_lock && !state->gaming_mode) {
        int dir_x = NONE, dir_y = NONE;
        float tx = axis_scan(state, cell, true,  ox, &dir_x);
        float ty = axis_scan(state, cell, false, oy, &dir_y);

        if (tx > 0 && (ty <= 0 || tx <= ty)) {
            c.cross = true; c.horizontal = true;
            c.direction = dir_x;
            c.other = clamp_other(state->plane_y + (int32_t)(oy * tx), cell, true);
        } else if (ty > 0) {
            c.cross = true; c.horizontal = false;
            c.direction = dir_y;
            c.other = clamp_other(state->plane_x + (int32_t)(ox * ty), cell, false);
        }
    }

    /* Advance inside the current cell; crossing handlers re-anchor. */
    int32_t nx = state->plane_x + ox, ny = state->plane_y + oy;
    cell_clamp(cell, &nx, &ny);
    set_plane_position(state, nx, ny);

    return c;
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

/* One relative report sliding the OS cursor across the arrangement boundary.
   The relative HID interface has 16-bit axes. Buttons forced to 0 to avoid
   carrying press state across the switch. */
static void send_relative_pulse(device_t *state, int16_t dx, int16_t dy) {
    mouse_report_t pulse = {.x = dx, .y = dy, .mode = RELATIVE, .buttons = 0};
    output_mouse_report(&pulse, state);
}

/* ========================= Crossing handlers ========================= */

/* Same-machine adjacent cells: the OS cursor has already crossed the
   physical boundary; flip the coordinate mode and re-anchor the plane. */
static void natural_handoff(device_t *state, bool into_relative, int32_t x, int32_t y) {
    rel_rem_x = rel_rem_y = 0;
    state->relative_mouse = into_relative;
    set_plane_position(state, x, y);
}

/* Corridor: no machine receives reports while inside; the leaving cursor
   stays parked at the exit edge (the movement report did that). */
static void corridor_enter(device_t *state, int32_t x, int32_t y) {
    state->corridor_mode = true;
    state->relative_mouse = false;
    set_plane_position(state, x, y);
}

static void corridor_exit_to_main(device_t *state, int32_t x) {
    state->corridor_mode = false;
    set_active_output(state, OUTPUT_A);
    state->relative_mouse = false;
    set_plane_position(state, x, MAX_SCREEN_COORD);
    mouse_report_t land = {.x = x, .y = MAX_SCREEN_COORD,
                           .mode = ABSOLUTE, .buttons = 0};
    output_mouse_report(&land, state);
}

static void corridor_exit_to_internal(device_t *state, int32_t y) {
    state->corridor_mode = false;
    set_active_output(state, OUTPUT_B);
    state->relative_mouse = false;
    set_plane_position(state, -1, y);
    mouse_report_t land = {.x = MAX_SCREEN_COORD - 1,
                           .y = y - BOTTOM_ROW_MIN,
                           .mode = ABSOLUTE, .buttons = 0};
    output_mouse_report(&land, state);
}

/* Cross-PC exits from the side display: Windows already clamped the leaving
   cursor at the side display's edge (it has no neighbouring screen there in
   the host topology), so the movement report doubled as the park. Land
   proportionally on the target primary - exact, no drift, no calibration. */
static void side_exit_to_main(device_t *state, int32_t y) {      /* P2, RIGHT */
    set_active_output(state, OUTPUT_A);
    state->relative_mouse = false;
    rel_rem_x = rel_rem_y = 0;
    set_plane_position(state, 0, y);
    mouse_report_t land = {.x = 0, .y = y, .mode = ABSOLUTE, .buttons = 0};
    output_mouse_report(&land, state);
}

static void side_exit_to_internal(device_t *state, int32_t x) {  /* P1, BOTTOM */
    set_active_output(state, OUTPUT_B);
    state->relative_mouse = false;
    rel_rem_x = rel_rem_y = 0;
    set_plane_position(state, x, BOTTOM_ROW_MIN);
    mouse_report_t land = {.x = x + MAX_SCREEN_COORD, .y = 0,
                           .mode = ABSOLUTE, .buttons = 0};
    output_mouse_report(&land, state);
}

/* Cross-PC entries into the (relative) side display: park the target
   machine's cursor absolutely on its primary, then send one relative pulse
   sized to land exactly at the kept coordinate. All the math lives on the
   mouse board - no inter-board coordinate exchange is needed. Pulses are
   computed in px and converted to counts via the target machine's pointer
   scale k, so landings are exact for any constant k (EPP must stay off). */

static void pulse_entry_internal_to_side(device_t *state, int32_t x) {  /* P1, TOP */
    uint16_t aux_w, aux_h;
    side_display_px(&aux_w, &aux_h);

    set_active_output(state, OUTPUT_A);
    state->relative_mouse = true;
    rel_rem_x = rel_rem_y = 0;

    /* Park the host cursor at the main display's bottom-left corner */
    mouse_report_t park = {.x = 0, .y = MAX_SCREEN_COORD,
                           .mode = ABSOLUTE, .buttons = 0};
    output_mouse_report(&park, state);

    /* One pulse across the arrangement boundary, landing a few pixels short
       of the side display's bottom edge at the kept column proportion. The
       pulse is sized in px, then converted to counts via the target OS
       pointer scale k (counts = px * 1000 / k). */
    uint16_t k = side_k_pm(state);
    int32_t col = x + MAX_SCREEN_COORD;   /* kept column, 0..MAX proportion */
    int32_t dx_px = (int32_t)((int64_t)col * aux_w / MAX_SCREEN_COORD) - aux_w;
    send_relative_pulse(state,
                        (int16_t)((int64_t)dx_px * 1000 / k),
                        (int16_t)(-(int64_t)(1 + PULSE_EDGE_MARGIN_PX) * 1000 / k));

    set_plane_position(state, x,
        MAX_SCREEN_COORD - (1 + PULSE_EDGE_MARGIN_PX) * MAX_SCREEN_COORD / aux_h);
}

static void pulse_entry_main_to_side(device_t *state, int32_t y) {     /* P2, LEFT */
    uint16_t aux_w, aux_h;
    side_display_px(&aux_w, &aux_h);

    set_active_output(state, OUTPUT_B);
    state->relative_mouse = true;
    rel_rem_x = rel_rem_y = 0;

    /* Park the laptop cursor on the internal screen's top edge, PARK_X_MARGIN_PX
       left of the shared corner (see the define comment). */
    mouse_report_t park = {
        .x = MAX_SCREEN_COORD - (int32_t)PARK_X_MARGIN_PX * MAX_SCREEN_COORD / aux_w,
        .y = 0,
        .mode = ABSOLUTE,
        .buttons = 0,
    };
    output_mouse_report(&park, state);

    /* Pulse up across the stacked boundary, landing at the kept height - but
       never closer to the shared edge than the safety margin. Sized in px,
       converted to counts via the target OS pointer scale k, then emitted as
       a burst of small reports (see PULSE_CHUNK_COUNTS). */
    uint16_t k = side_k_pm(state);
    int32_t dy_px = (int32_t)((int64_t)(y - MAX_SCREEN_COORD) * aux_h / MAX_SCREEN_COORD);
    if (dy_px > -(1 + PULSE_EDGE_MARGIN_PX))
        dy_px = -(1 + PULSE_EDGE_MARGIN_PX);

    int32_t counts = (int32_t)((int64_t)dy_px * 1000 / k);

    /* Hand the burst to the paced emitter (process_mouse_queue_task) instead
       of queueing it flat-out: see PULSE_CHUNK_PERIOD. */
    state->pulse_wait = PULSE_CHUNK_PERIOD;
    state->pulse_left = counts;

    /* Anchor the ledger where the burst actually lands: PARK_X_MARGIN_PX left
       of the shared edge, proportional row. */
    set_plane_position(state,
        -1 - (int32_t)PARK_X_MARGIN_PX * MAX_SCREEN_COORD / aux_w,
        MAX_SCREEN_COORD + (int32_t)((int64_t)dy_px * MAX_SCREEN_COORD / aux_h));
}

static void handle_crossing(device_t *state, plane_cell_e from, crossing_t *c) {
    bool host = (state->layout_profile == LAYOUT_PROFILE_HOST);

    /* Any new crossing invalidates a still-running pulse burst - its ledger
       anchor belonged to the previous entry and leftover chunks would only
       pollute the fresh one. */
    state->pulse_left = 0;

    if (c->horizontal) {
        if (c->direction == LEFT) {
            if (from == CELL_MAIN) {
                if (host)
                    natural_handoff(state, true, -1, c->other);        /* P1: main -> side */
                else
                    pulse_entry_main_to_side(state, c->other);         /* P2: park + pulse */
            } else { /* CELL_VIRTUAL */
                corridor_exit_to_internal(state, c->other);
            }
        } else { /* RIGHT */
            if (from == CELL_SIDE) {
                if (host)
                    natural_handoff(state, false, 0, c->other);        /* P1: side -> main */
                else
                    side_exit_to_main(state, c->other);                /* P2: direct exit  */
            } else { /* CELL_INTERNAL */
                corridor_enter(state, 0, c->other);
            }
        }
    } else {
        if (c->direction == BOTTOM) {
            if (from == CELL_SIDE) {
                if (host)
                    side_exit_to_internal(state, c->other);            /* P1: direct exit  */
                else
                    natural_handoff(state, false, c->other, BOTTOM_ROW_MIN); /* P2 */
            } else { /* CELL_MAIN */
                corridor_enter(state, c->other, BOTTOM_ROW_MIN);
            }
        } else { /* TOP */
            if (from == CELL_INTERNAL) {
                if (host)
                    pulse_entry_internal_to_side(state, c->other);     /* P1: park + pulse */
                else
                    natural_handoff(state, true, c->other, MAX_SCREEN_COORD); /* P2 */
            } else { /* CELL_VIRTUAL */
                corridor_exit_to_main(state, c->other);
            }
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
        .wheel   = values->wheel,
        .pan     = values->pan,
        .mode    = ABSOLUTE,
    };

    /* Relative mode: Windows multiple desktops workaround, the gaming mode
       passthru, and the (relative-only) side display cell. */
    if (state->relative_mouse || state->gaming_mode ||
        cell_of(state->plane_x, state->plane_y) == CELL_SIDE) {
        mouse_report.x = values->move_x;
        mouse_report.y = values->move_y;
        mouse_report.mode = RELATIVE;
    } else {
        /* pointer_x/y already hold the current cell's screen-local coords */
        mouse_report.x = state->pointer_x;
        mouse_report.y = state->pointer_y;
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

    plane_cell_e from = cell_of(state->plane_x, state->plane_y);

    /* Move the plane and detect edge crossings (if any). */
    crossing_t c = detect_and_update(state, &values);

    /* Emit the movement report - on exits it doubles as the park report at
       the leaving edge (the OS clamps it there). Suppressed inside the
       corridor, where no machine owns the cursor; crossing handlers that
       need reports send them themselves. */
    if (!state->corridor_mode) {
        mouse_report_t report = create_mouse_report(state, &values);
        output_mouse_report(&report, state);
    }

    /* We use the mouse to switch cells: cross an axis of the plane */
    if (c.cross)
        handle_crossing(state, from, &c);
}

/* ==================================================== *
 * Mouse Queue Section
 * ==================================================== */

/* Emit one chunk of a pending pulse burst, paced to PULSE_CHUNK_PERIOD so the
   injected speed stays inside the regime the target machine's input stack has
   been measured to pass faithfully (see the define). */
static void pulse_burst_tick(device_t *state) {
    if (!state->pulse_left)
        return;

    if (++state->pulse_wait < PULSE_CHUNK_PERIOD)
        return;
    state->pulse_wait = 0;

    int16_t chunk = (int16_t)clamp_i32(state->pulse_left, -PULSE_CHUNK_COUNTS, PULSE_CHUNK_COUNTS);
    send_relative_pulse(state, 0, chunk);
    state->pulse_left -= chunk;
}

void process_mouse_queue_task(device_t *state) {
    mouse_report_t report = {0};

    /* We need to be connected to the host to send messages */
    if (!state->tud_connected)
        return;

    /* Advance a pending pulse burst (independent of the queue contents) */
    pulse_burst_tick(state);

    /* Peek first, if there is anything there... */
    if (!queue_try_peek(&state->mouse_queue, &report))
        return;

    /* If we are suspended, let's wake the host up */
    if (tud_suspended())
        tud_remote_wakeup();

    /* Decay the settle window opened by the last ABSOLUTE submission */
    if (state->abs_settle)
        state->abs_settle--;

    /* If it's not ready, we'll try on the next pass. Park and pulse travel on
       DIFFERENT interrupt endpoints (ITF0 absolute / ITF1 relative) - endpoint
       readiness says nothing about what the host has already consumed, and
       both endpoints are polled within the same USB frame, so a relative
       report submitted right after an absolute one can overtake it. The
       settle window holds RELATIVE reports for a few ms after any ABSOLUTE
       submission, which strictly orders park-then-pulse portal entries (and
       is imperceptible: it only ever bites on mode transitions). */
    if (report.mode == RELATIVE) {
        if (state->abs_settle
            || !tud_hid_n_ready(ITF_NUM_HID) || !tud_hid_n_ready(ITF_NUM_HID_REL_M))
            return;
    } else if (!tud_hid_n_ready(ITF_NUM_HID))
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
    if (succeeded) {
        queue_try_remove(&state->mouse_queue, &report);
        if (report.mode == ABSOLUTE)
            state->abs_settle = REL_HOLD_TICKS;
    }
}

void queue_mouse_report(mouse_report_t *report, device_t *state) {
    /* It wouldn't be fun to queue up a bunch of messages and then dump them all on host */
    if (!state->tud_connected)
        return;

    queue_try_add(&state->mouse_queue, report);
}
