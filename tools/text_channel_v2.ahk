#Requires AutoHotkey v2.0
#SingleInstance Force
; ---------------------------------------------------------------------------
; text_channel_v2.ahk - DeskHop cross-board text transfer (firmware v0.80.3+)
; AHK v2 port of tools/text_channel.ahk; behaviour aligned with the Rust exe:
;
;   Ctrl+Shift+1 : clear clipboard, ^c the current selection, push to the
;                  OTHER machine's Pico (8 KB single-shot buffer)
;   Ctrl+Shift+2 : pull the buffered text from THIS machine's Pico, put it
;                  on the clipboard and paste it at the caret (^v)
;   Ctrl+Shift+0 : exit
;
; No hotkey-space tray logic - the v2 script is intentionally lean (the Rust
; exe is the full-featured build). Only hid.dll / setupapi.dll DllCalls, no
; driver. Runs on both machines.
;
; Wire protocol (must match src/text_channel.c):
;   USB packet = report_id(6) + 0xAA + 0x55 + type + data[8] + xor(data[8])
;   TinyUSB strips the report id on SET_REPORT and fills it on GET_REPORT.
;   GET response data[7] = 0x80|n marks the last chunk with n bytes (n=1..7);
;   0 means a full 7-byte chunk with more to follow.
; ---------------------------------------------------------------------------

; ========================= Configuration =========================

REPORT_ID   := 6
VID_PID     := "vid_1209&pid_c000"
IFACE       := "mi_02"
USB_PKT_LEN := 13
MAX_TEXT    := 8191

TC_PUSH_BEGIN := 1
TC_PUSH_DATA  := 2
TC_PULL_REQ   := 3
TC_PULL_DATA  := 5
TC_PULL_EMPTY := 0xFF

gHid := 0    ; cached device handle, 0 = reopen on next use

^+1::PushText()
^+2::PullText()
^+0::ExitApp

; ========================= Push (Ctrl+Shift+1) =========================

PushText() {
    global gHid
    h := GetHid()
    if !h {
        Tip("DeskHop vendor HID not found (VID_1209&PID_C000&MI_02)")
        return
    }

    A_Clipboard := ""      ; clear, then copy the current selection.
    Send("^c")             ; v2 Send temporarily releases Shift/Ctrl as needed,
    if !ClipWait(1.5) {    ; so this is not Ctrl+Shift+C.
        Tip("Copy failed - is something selected?")
        return
    }
    text := A_Clipboard
    if (text = "") {
        Tip("Clipboard is empty")
        return
    }

    ; UTF-8 encode; StrPut with target buffer returns bytes incl. terminator
    raw := Buffer(StrPut(text, "UTF-8"), 0)
    StrPut(text, raw, "UTF-8")
    total := raw.Size - 1
    truncated := false
    if (total > MAX_TEXT) {
        total := MAX_TEXT
        ; back off to a UTF-8 boundary: first dropped byte must not be a
        ; continuation byte (0x80..0xBF)
        while (total > 0 && (NumGet(raw, total, "uchar") & 0xC0) = 0x80)
            total--
        truncated := true
    }

    ; BEGIN packet: total length, little endian
    begin := Buffer(8, 0)
    NumPut("uchar", total & 0xFF, begin, 0)
    NumPut("uchar", (total >> 8) & 0xFF, begin, 1)
    if !SendPacket(h, TC_PUSH_BEGIN, begin) {
        gHid := 0          ; stale handle - reopen on the next attempt
        Tip("Send failed (error " A_LastError ")")
        return
    }

    ; DATA packets: seq + up to 7 bytes of text each
    seq := 0
    sent := 0
    while (sent < total) {
        chunk := Min(total - sent, 7)
        data := Buffer(8, 0)
        NumPut("uchar", seq, data, 0)
        DllCall("RtlMoveMemory", "ptr", data.Ptr + 1, "ptr", raw.Ptr + sent, "ptr", chunk)
        if !SendPacket(h, TC_PUSH_DATA, data) {
            gHid := 0
            Tip("Send failed (error " A_LastError ")")
            return
        }
        sent += chunk
        seq := (seq + 1) & 0xFF
        if (Mod(seq, 50) = 0)
            Tip("Sending... " Round(sent * 100 / total) "%")
    }
    Tip(truncated ? "Sent " total " bytes (truncated at 8 KB)"
                  : "Sent " total " bytes to the other machine")
}

; ========================= Pull (Ctrl+Shift+2) =========================

PullText() {
    global gHid
    h := GetHid()
    if !h {
        Tip("DeskHop vendor HID not found (VID_1209&PID_C000&MI_02)")
        return
    }

    ; PULL_REQ restarts the read side, then GET_REPORT returns the data
    req := Buffer(8, 0)
    if !SendPacket(h, TC_PULL_REQ, req) {
        gHid := 0
        Tip("Pull failed (error " A_LastError ")")
        return
    }

    raw := Buffer(MAX_TEXT + 1, 0)     ; zero-filled -> stays null-terminated
    got := 0
    rbuf := Buffer(USB_PKT_LEN, 0)
    NumPut("uchar", REPORT_ID, rbuf, 0)   ; GET_REPORT expects the report id in byte 0
    Loop 1300 {
        if !DllCall("hid.dll\HidD_GetInputReport", "ptr", h, "ptr", rbuf, "uint", USB_PKT_LEN, "int") {
            gHid := 0
            Tip("Pull failed (error " A_LastError ")")
            return
        }
        ; rbuf = [report_id][0xAA][0x55][type][d0..d7][checksum]
        if (NumGet(rbuf, 1, "uchar") != 0xAA || NumGet(rbuf, 2, "uchar") != 0x55) {
            Tip("Pull failed (bad framing)")
            return
        }
        type := NumGet(rbuf, 3, "uchar")
        if (type = TC_PULL_EMPTY)
            break
        if (type != TC_PULL_DATA) {
            Tip("Pull failed (unexpected type " type ")")
            return
        }
        sum := 0
        Loop 8
            sum ^= NumGet(rbuf, 3 + A_Index, "uchar")
        if (sum != NumGet(rbuf, 12, "uchar")) {
            Tip("Pull failed (checksum mismatch)")
            return
        }
        flags := NumGet(rbuf, 11, "uchar")      ; data[7]
        eof := (flags & 0x80) != 0
        chunk := eof ? (flags & 0x7F) : 7
        if (chunk > 0) {
            DllCall("RtlMoveMemory", "ptr", raw.Ptr + got, "ptr", rbuf.Ptr + 4, "ptr", chunk)
            got += chunk
        }
        if eof
            break
    }

    if (got = 0) {
        Tip("Nothing to pull")
        return
    }
    text := StrGet(raw.Ptr, "UTF-8")
    A_Clipboard := text
    Sleep(80)
    Send("^v")     ; paste at the caret (= last clicked input position)
    Tip("Pasted " got " bytes at the caret")
}

; ========================= Packet helpers =========================
; pkt: 13-byte buffer. SendPacket writes the full framing: report id at 0,
; START bytes (0xAA 0x55) at 1/2, type at 3, checksum at 12.
; data: Buffer(8) copied to offsets 4..11.
SendPacket(h, type, data) {
    pkt := Buffer(USB_PKT_LEN, 0)
    NumPut("uchar", REPORT_ID, pkt, 0)   ; TinyUSB strips byte 0 only if it equals the report id
    NumPut("uchar", 0xAA, pkt, 1)        ; START1
    NumPut("uchar", 0x55, pkt, 2)        ; START2
    NumPut("uchar", type, pkt, 3)
    DllCall("RtlMoveMemory", "ptr", pkt.Ptr + 4, "ptr", data.Ptr, "ptr", 8)
    ; NB: v2.0 NumGet has NO type-first form (only NumPut does) - the
    ; argument order here must stay (Target, Offset, Type).
    sum := 0
    Loop 8
        sum ^= NumGet(pkt, 3 + A_Index, "uchar")
    NumPut("uchar", sum, pkt, 12)
    return DllCall("hid.dll\HidD_SetOutputReport", "ptr", h, "ptr", pkt, "uint", USB_PKT_LEN, "int") != 0
}

; ========================= Device opening =========================
; Opens the DeskHop vendor HID collection (interface MI_02). Result is cached;
; set gHid := 0 to force a reopen.
GetHid() {
    global gHid
    if gHid
        return gHid
    gHid := OpenVendorHid()
    return gHid
}

OpenVendorHid() {
    guid := Buffer(16, 0)
    DllCall("hid.dll\HidD_GetHidGuid", "ptr", guid)
    info := DllCall("setupapi.dll\SetupDiGetClassDevsW", "ptr", guid, "ptr", 0, "ptr", 0, "uint", 0x12, "ptr")
    if !info
        return 0
    found := 0
    ifData := Buffer(32, 0)                 ; SP_DEVICE_INTERFACE_DATA, x64
    NumPut("uint", 32, ifData, 0)
    Loop 64 {
        if !DllCall("setupapi.dll\SetupDiEnumDeviceInterfaces", "ptr", info, "ptr", 0, "ptr", guid, "uint", A_Index - 1, "ptr", ifData)
            break
        need := 0
        DllCall("setupapi.dll\SetupDiGetDeviceInterfaceDetailW", "ptr", info, "ptr", ifData, "ptr", 0, "uint", 0, "uint*", &need, "ptr", 0)
        if !need
            continue
        detail := Buffer(need, 0)
        NumPut("uint", 8, detail, 0)        ; cbSize, x64
        if !DllCall("setupapi.dll\SetupDiGetDeviceInterfaceDetailW", "ptr", info, "ptr", ifData, "ptr", detail, "uint", need, "ptr", 0, "ptr", 0)
            continue
        path := StrGet(detail.Ptr + 4, "UTF-16")   ; DevicePath, null-terminated wide string
        if InStr(path, VID_PID) && InStr(path, IFACE) {
            found := DllCall("kernel32.dll\CreateFileW", "wstr", path, "uint", 0xC0000000, "uint", 3, "ptr", 0, "uint", 3, "uint", 0, "ptr", 0, "ptr")
            break
        }
    }
    DllCall("setupapi.dll\SetupDiDestroyDeviceInfoList", "ptr", info)
    return (found = -1) ? 0 : found         ; INVALID_HANDLE_VALUE -> not found
}

; ========================= Feedback =========================

Tip(msg) {
    ToolTip(msg)
    SetTimer(() => ToolTip(), -2500)
}
