; ---------------------------------------------------------------------------
; DeskHop Text Channel  (v0.80.3 firmware)
; ---------------------------------------------------------------------------
; Transfers a block of text (<= 8191 bytes UTF-8, ~2700 Chinese chars) between
; the two machines connected to a DeskHop, via the vendor HID interface.
;
;   Ctrl+Shift+S : push the local clipboard to the OTHER machine (Send)
;   Ctrl+Shift+F : pull the buffered text from THIS machine's Pico (Fetch)
;
; Both machines run the same script. Typical flow: select text on machine A,
; Ctrl+Shift+S (or copy first), then switch to machine B and press Ctrl+Shift+F.
; The firmware buffer is single-shot and burns after reading.
;
; Requires AutoHotkey v1.1 (classic). No driver needed - uses hid.dll.
; ---------------------------------------------------------------------------

#Requires AutoHotkey v1.1
#NoEnv
#SingleInstance Force
SetBatchLines -1

; ========================= Configuration =========================
; 2026-09-30: S/F were taken on the output-A machine (verified by probing
; RegisterHotKey for all Ctrl+Alt+letter/digit combos). Ctrl+Shift pool has
; only A/W busy there, so S/F are free under Ctrl+Shift.
SEND_HOTKEY := "^+s"                  ; Ctrl+Shift+S : push (Send)
PULL_HOTKEY := "^+f"                  ; Ctrl+Shift+F : pull (Fetch)
VID_PID     := "vid_1209&pid_c000"    ; DeskHop normal-mode device, interface MI_02
REPORT_ID   := 6                      ; REPORT_ID_VENDOR in the firmware
PKT_LEN     := 12                     ; raw UART-style packet length
USB_PKT_LEN := 13                     ; PKT_LEN + report id byte
MAX_TEXT    := 8191                   ; firmware buffer is 8192 bytes

; packet type bytes (must match src/include/text_channel.h)
TC_PUSH_BEGIN := 1                    ; data[0..1] = total length, little endian
TC_PUSH_DATA  := 2                    ; data[0] = seq, data[1..7] = text
TC_PULL_REQ   := 3                    ; restart the read side
TC_PULL_DATA  := 5                    ; data[0..6] = text, data[7] = 0x80|len on last chunk
TC_PULL_EMPTY := 0xFF                 ; nothing buffered

; ========================= Hotkeys =========================
Hotkey, %SEND_HOTKEY%, SendClipboard
Hotkey, %PULL_HOTKEY%, PullText
return

; ========================= Push (Ctrl+Shift+S) =========================
SendClipboard:
    h := GetHid()
    if (!h) {
        Tip("DeskHop vendor HID not found (VID_1209&PID_C000&MI_02)")
        return
    }

    text := Clipboard
    total := StrPut(text, "UTF-8") - 1
    if (total <= 0) {
        Tip("Clipboard is empty (or not text)")
        return
    }
    truncated := false
    if (total > MAX_TEXT) {
        total := MAX_TEXT
        truncated := true
    }

    ; UTF-8 encode the text into a byte buffer
    VarSetCapacity(raw, total + 1, 0)
    StrPut(text, &raw, total + 1, "UTF-8")

    ; BEGIN packet: total length, little endian
    VarSetCapacity(pkt, USB_PKT_LEN, 0)
    NumPut(total & 0xFF, pkt, 4, "uchar")
    NumPut((total >> 8) & 0xFF, pkt, 5, "uchar")
    if (!SendPacket(h, TC_PUSH_BEGIN, pkt))
        return

    ; DATA packets: seq + 7 bytes of text each
    seq := 0
    sent := 0
    while (sent < total) {
        chunk := (total - sent < 7) ? (total - sent) : 7
        VarSetCapacity(pkt, USB_PKT_LEN, 0)
        NumPut(seq & 0xFF, pkt, 4, "uchar")
        DllCall("RtlMoveMemory", "ptr", &pkt + 5, "ptr", &raw + sent, "ptr", chunk)
        if (!SendPacket(h, TC_PUSH_DATA, pkt))
            return
        sent += chunk
        seq := (seq + 1) & 0xFF
        if (Mod(seq, 50) = 0)
            Tip("Sending... " . Round(sent * 100 / total) . "%")
    }
    tipText := "Sent " . total . " bytes to the other machine"
    if (truncated)
        tipText .= " (truncated at 8 KB)"
    Tip(tipText)
return

; ========================= Pull (Ctrl+Shift+F) =========================
PullText:
    h := GetHid()
    if (!h) {
        Tip("DeskHop vendor HID not found (VID_1209&PID_C000&MI_02)")
        return
    }

    ; PULL_REQ restarts the read side, then GET_REPORT returns the data
    VarSetCapacity(pkt, USB_PKT_LEN, 0)
    if (!SendPacket(h, TC_PULL_REQ, pkt))
        return

    VarSetCapacity(raw, MAX_TEXT + 1, 0)
    got := 0
    VarSetCapacity(rbuf, USB_PKT_LEN, 0)
    NumPut(REPORT_ID, rbuf, 0, "uchar")   ; GET_REPORT expects the report id in byte 0
    Loop, 1300
    {
        if (!DllCall("hid.dll\HidD_GetInputReport", "ptr", h, "ptr", &rbuf, "uint", USB_PKT_LEN))
        {
            g_hHid := 0                ; stale handle - reopen on the next attempt
            Tip("Pull failed (error " . A_LastError . ")")
            return
        }
        ; rbuf = [report_id][0xAA][0x55][type][d0..d7][checksum]
        if (NumGet(rbuf, 1, "uchar") != 0xAA || NumGet(rbuf, 2, "uchar") != 0x55)
        {
            Tip("Pull failed (bad packet framing)")
            return
        }
        type := NumGet(rbuf, 3, "uchar")
        if (type = TC_PULL_EMPTY)
            break
        if (type != TC_PULL_DATA)
        {
            Tip("Pull failed (unexpected type " . type . ")")
            return
        }
        ; checksum over data[0..7], stored in byte 12
        sum := 0
        Loop, 8
            sum ^= NumGet(rbuf, 3 + A_Index, "uchar")
        if (sum != NumGet(rbuf, 12, "uchar"))
        {
            Tip("Pull failed (checksum mismatch)")
            return
        }
        ; data[7] flags: 0x80|n = final chunk with n bytes, 0 = full chunk
        flags := NumGet(rbuf, 11, "uchar")
        eof := (flags & 0x80) != 0
        chunk := eof ? (flags & 0x7F) : 7
        DllCall("RtlMoveMemory", "ptr", &raw + got, "ptr", &rbuf + 4, "ptr", chunk)
        got += chunk
        if (eof)
            break
        if (Mod(A_Index, 50) = 0)
            Tip("Receiving... " . got . " bytes")
    }

    if (got = 0) {
        Tip("Nothing to pull (no text was sent to this machine)")
        return
    }

    text := StrGet(&raw, got, "UTF-8")
    Clipboard := text
    Tip("Pulled " . got . " bytes to clipboard")
return

; ========================= Packet helpers =========================
; pkt: 13-byte buffer, report id at 0, START bytes at 1/2, type at 3
; filled by caller: data[0..7] at offsets 4..11; checksum appended here.
SendPacket(h, type, pkt) {
    global USB_PKT_LEN, g_hHid
    NumPut(type, pkt, 3, "uchar")
    sum := 0
    Loop, 8
        sum ^= NumGet(pkt, 3 + A_Index, "uchar")
    NumPut(sum, pkt, 12, "uchar")
    if (!DllCall("hid.dll\HidD_SetOutputReport", "ptr", h, "ptr", &pkt, "uint", USB_PKT_LEN))
    {
        g_hHid := 0                    ; stale handle - reopen on the next attempt
        Tip("Send failed (error " . A_LastError . ")")
        return false
    }
    return true
}

; ========================= Device opening =========================
; Opens the DeskHop vendor HID collection (interface MI_02). Result is cached;
; call GetHid(0) to force a reopen.
GetHid() {
    global g_hHid
    if (g_hHid)
        return g_hHid
    g_hHid := OpenVendorHid()
    return g_hHid
}

OpenVendorHid() {
    global VID_PID
    VarSetCapacity(guid, 16, 0)
    DllCall("hid.dll\HidD_GetHidGuid", "ptr", &guid)

    ; DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    hInfo := DllCall("setupapi\SetupDiGetClassDevs", "ptr", &guid, "ptr", 0, "ptr", 0, "uint", 0x12, "ptr")
    if (hInfo = -1 || hInfo = 0)
        return 0

    cbIfData := (A_PtrSize = 8) ? 32 : 16
    VarSetCapacity(ifData, cbIfData, 0)
    NumPut(cbIfData, ifData, 0, "uint")

    path := ""
    Loop
    {
        if (!DllCall("setupapi\SetupDiEnumDeviceInterfaces", "ptr", hInfo, "ptr", 0, "ptr", &guid, "uint", A_Index - 1, "ptr", &ifData))
            break

        ; Query required size, then fetch the device path
        need := 0
        DllCall("setupapi\SetupDiGetDeviceInterfaceDetailW", "ptr", hInfo, "ptr", &ifData, "ptr", 0, "uint", 0, "uint*", need, "ptr", 0)
        if (need = 0)
            continue
        VarSetCapacity(detail, need, 0)
        NumPut((A_PtrSize = 8) ? 8 : 6, detail, 0, "uint")
        if (!DllCall("setupapi\SetupDiGetDeviceInterfaceDetailW", "ptr", hInfo, "ptr", &ifData, "ptr", &detail, "uint", need, "uint*", 0, "ptr", 0))
            continue

        devPath := StrGet(&detail + 4, need - 4, "utf-16")
        ; Match the vendor interface of the DeskHop: ...\hid#vid_1209&pid_c000&mi_02#...
        if (InStr(devPath, VID_PID) && InStr(devPath, "mi_02"))
        {
            path := devPath
            break
        }
    }
    DllCall("setupapi\SetupDiDestroyDeviceInfoList", "ptr", hInfo)

    if (path = "")
        return 0

    VarSetCapacity(pathBuf, (StrLen(path) + 1) * 2, 0)
    StrPut(path, &pathBuf, "utf-16")

    ; GENERIC_READ|GENERIC_WRITE, shared read/write, OPEN_EXISTING
    h := DllCall("CreateFileW", "ptr", &pathBuf, "uint", 0xC0000000, "uint", 3, "ptr", 0, "uint", 3, "uint", 0, "ptr", 0, "ptr")
    if (h = -1 || h = 0)
        return 0
    return h
}

; ========================= Cosmetic =========================
Tip(msg) {
    ToolTip, DeskHop: %msg%
    SetTimer, ClearTip, -1500
}

ClearTip:
    ToolTip
return
