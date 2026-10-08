#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeskHop text channel - minimal host tool (firmware v0.80.3+).

Pure standard library (ctypes over hid.dll / setupapi.dll), nothing to
install. The laptop-friendly, zero-footprint variant of the text channel:
no hotkeys, no background process - runs once per invocation.

Usage:
  python text_channel_min.py push [file|-]   push clipboard / file / stdin
  python text_channel_min.py pull            pull buffered text -> clipboard + stdout

Protocol (must match src/text_channel.c):
  USB packet = report_id(6) + 0xAA + 0x55 + type + data[8] + xor(data[8])
  TinyUSB strips the report id on SET_REPORT and fills it on GET_REPORT.
  GET response data[7] = 0x80|n marks the last chunk with n bytes (n=1..7);
  0 means a full 7-byte chunk with more to follow.
"""

import ctypes
import sys

REPORT_ID = 6
VID_PID = "vid_1209&pid_c000"
IFACE = "mi_02"
MAX_TEXT = 8191

TC_PUSH_BEGIN = 1   # data[0..1] = total length, little endian
TC_PUSH_DATA = 2    # data[0] = seq, data[1..7] = text
TC_PULL_REQ = 3     # restart the read side
TC_PULL_DATA = 5    # GET_REPORT response: data[0..6] text, data[7] = 0x80|n
TC_PULL_EMPTY = 0xFF

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 2
INVALID_HANDLE = 18446744073709551615  # (unsigned)-1 on x64

if sys.platform != "win32":
    sys.exit("Windows only.")

hid = ctypes.WinDLL("hid.dll")
setup = ctypes.WinDLL("setupapi.dll")
k32 = ctypes.WinDLL("kernel32")
u32 = ctypes.WinDLL("user32")

hid.HidD_SetOutputReport.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
hid.HidD_SetOutputReport.restype = ctypes.c_ubyte          # BOOLEAN = 1 byte
hid.HidD_GetInputReport.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
hid.HidD_GetInputReport.restype = ctypes.c_ubyte

k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalAlloc.argtypes = [ctypes.c_uint32, ctypes.c_size_t]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
k32.GlobalFree.argtypes = [ctypes.c_void_p]

u32.GetClipboardData.restype = ctypes.c_void_p
u32.GetClipboardData.argtypes = [ctypes.c_uint32]
u32.SetClipboardData.restype = ctypes.c_void_p
u32.SetClipboardData.argtypes = [ctypes.c_uint32, ctypes.c_void_p]

setup.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setup.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]


class GUID(ctypes.Structure):
    _fields_ = [("d0", ctypes.c_uint32), ("d1", ctypes.c_uint16),
                ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint8 * 8)]


class IfData(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("guid", GUID),
                ("flags", ctypes.c_uint32), ("reserved", ctypes.c_size_t)]


def open_vendor_hid():
    """Enumerate present HID interfaces and open the DeskHop vendor collection."""
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    info = setup.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)  # PRESENT|DEVICEINTERFACE
    if not info:
        return None
    try:
        for idx in range(64):
            ifd = IfData()
            ifd.cb = ctypes.sizeof(ifd)
            if not setup.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), idx, ctypes.byref(ifd)):
                break
            need = ctypes.c_uint32()
            setup.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            if not need.value:
                continue
            buf = ctypes.create_string_buffer(need.value)
            ctypes.memmove(buf, ctypes.c_uint32(8), 4)  # cbSize = 8 on x64
            if not setup.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(ifd), buf, need, None, None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4).lower()
            if VID_PID in path and IFACE in path:
                h = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)  # RW, share, open existing
                if h and h != INVALID_HANDLE:
                    return h
                return None
    finally:
        setup.SetupDiDestroyDeviceInfoList(info)
    return None


def send_packet(h, t, data):
    pkt = bytes([REPORT_ID, 0xAA, 0x55, t]) + bytes(data)
    chk = 0
    for b in data:
        chk ^= b
    pkt += bytes([chk])
    return bool(hid.HidD_SetOutputReport(h, pkt, len(pkt)))


def push_text(h, text):
    raw = text.encode("utf-8")
    total = min(len(raw), MAX_TEXT)
    if total == 0:
        raise RuntimeError("nothing to push (empty text)")
    if total < len(raw):
        # back off to a UTF-8 boundary: first dropped byte must not be a continuation
        while total > 0 and raw[total] & 0xC0 == 0x80:
            total -= 1
    d = bytes([total & 0xFF, (total >> 8) & 0xFF, 0, 0, 0, 0, 0, 0])
    if not send_packet(h, TC_PUSH_BEGIN, d):
        raise OSError("SET_REPORT failed (BEGIN)")
    seq, sent = 0, 0
    while sent < total:
        d = bytes([seq]) + raw[sent:sent + 7].ljust(7, b"\x00")
        if not send_packet(h, TC_PUSH_DATA, d):
            raise OSError("SET_REPORT failed at byte %d" % sent)
        sent += 7
        seq = (seq + 1) & 0xFF
        if seq % 50 == 0:
            print("  %d%%" % (sent * 100 // total), file=sys.stderr)
    return total


def pull_text(h):
    if not send_packet(h, TC_PULL_REQ, bytes(8)):
        raise OSError("SET_REPORT failed (PULL_REQ)")
    out = bytearray()
    rbuf = (ctypes.c_ubyte * 13)()
    rbuf[0] = REPORT_ID  # GET_REPORT expects the report id in byte 0
    for _ in range(1300):
        if not hid.HidD_GetInputReport(h, rbuf, 13):
            raise OSError("GET_REPORT failed (stale handle? re-run)")
        pkt = bytes(rbuf)
        if pkt[1:3] != b"\xaa\x55":
            raise RuntimeError("bad packet framing")
        t = pkt[3]
        if t == TC_PULL_EMPTY:
            break
        if t != TC_PULL_DATA:
            raise RuntimeError("unexpected packet type %d" % t)
        chk = 0
        for b in pkt[4:12]:
            chk ^= b
        if chk != pkt[12]:
            raise RuntimeError("checksum mismatch")
        flags = pkt[11]                      # data[7]
        eof = flags & 0x80
        chunk = (flags & 0x7F) if eof else 7
        if chunk:
            out += pkt[4:4 + chunk]
        if eof:
            break
    if not out:
        return None
    return out.decode("utf-8", errors="replace")


def clip_get():
    if not u32.OpenClipboard(0):
        return None
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = k32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def clip_set(text):
    if not u32.OpenClipboard(0):
        return False
    try:
        u32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = k32.GlobalLock(h)
        if not p:
            k32.GlobalFree(h)
            return False
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(CF_UNICODETEXT, h):
            k32.GlobalFree(h)
            return False
        return True
    finally:
        u32.CloseClipboard()


def main(argv):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    cmd = argv[1] if len(argv) > 1 else ""
    h = open_vendor_hid()
    if not h:
        sys.exit("DeskHop vendor HID not found (VID_1209&PID_C000&MI_02).\n"
                 "Is the board running v0.80.3 and NOT in config mode?")
    try:
        if cmd == "push":
            if len(argv) > 2:
                src = argv[2]
                text = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
            else:
                text = clip_get()
                if text is None:
                    sys.exit("Clipboard has no text.")
            n = push_text(h, text)
            print("pushed %d bytes to the other board" % n)
        elif cmd == "pull":
            text = pull_text(h)
            if text is None:
                print("nothing buffered on this board's Pico")
                return
            clip_set(text)
            print(text)
            print("--- %d chars pulled (also placed on the clipboard)" % len(text), file=sys.stderr)
        else:
            print(__doc__.strip())
    finally:
        k32.CloseHandle(h)


if __name__ == "__main__":
    main(sys.argv)
