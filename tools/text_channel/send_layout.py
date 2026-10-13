#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeskHop layout profile select - manual TC_LAYOUT_SELECT (type=4) sender.

Pure standard library (ctypes over hid.dll / setupapi.dll), nothing to
install. Debug tool for the KVM-state watcher: sends the same vendor packet
the resident tray tool sends, so you can tell apart "the watcher's packet
is bad/timed badly" from "the firmware doesn't accept type=4 at all".

Usage:
  python send_layout.py --probe         read board diag only (role, profile,
                                        type4 counter, config mode) - v0.80.8+
  python send_layout.py 0               switch to HOST   (P1, side -> PC)
  python send_layout.py 1               switch to LAPTOP (P2, side -> laptop)
  python send_layout.py 1 --repeat 5 --interval 250
                                        send it 5 times, 250 ms apart

Every send prints [before]/[after] probe lines. If type4_seen does not
increase, the packet never reached the firmware - the USB layer or an
older firmware (pre-0.80.8, no diag) is the problem, not the command.

Notes:
  - v0.80.8+: sending the profile the board already has still re-announces
    it to the peer, so you can use this to REPAIR a stale other board
    (e.g. after a dropped sync): just send the profile that looks correct.
  - v0.80.7+ blinks the RECEIVING board's LED when the peer applies the
    sync, so watch the laptop-side Pico LED to confirm the sync landed.
  - If one shot fails to move the other board but --repeat 5 does, the
    UART TX queue was saturated by mouse relay traffic at send time.
"""

import argparse
import ctypes
import sys
import time

REPORT_ID = 6
VID_PID = "vid_1209&pid_c000"
IFACE = "mi_02"

TC_LAYOUT_SELECT = 4  # data[0] = target profile (0=HOST, 1=LAPTOP)

INVALID_HANDLE = 18446744073709551615  # (unsigned)-1 on x64

if sys.platform != "win32":
    sys.exit("Windows only.")

hid = ctypes.WinDLL("hid.dll")
setup = ctypes.WinDLL("setupapi.dll")
k32 = ctypes.WinDLL("kernel32")

hid.HidD_SetOutputReport.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
hid.HidD_SetOutputReport.restype = ctypes.c_ubyte
hid.HidD_GetInputReport.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
hid.HidD_GetInputReport.restype = ctypes.c_ubyte

k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
k32.CloseHandle.argtypes = [ctypes.c_void_p]

setup.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setup.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                   ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
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
    info = setup.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)
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
            buf[0:4] = (8).to_bytes(4, "little")  # cbSize = 8 on x64
            if not setup.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(ifd), buf, need, None, None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4).lower()
            if VID_PID in path and IFACE in path:
                h = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)
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


ROLE_NAME = {0: "A (HOST board)", 1: "B (LAPTOP board)"}


def probe(h):
    """GET_REPORT round trip. The PULL_EMPTY response piggybacks board
    diagnostics (v0.80.8+): data[0]=layout_profile, data[1]=type4 count,
    data[2]=active_output, data[3]=bit0 config_mode / bits1+ board role."""
    rbuf = (ctypes.c_ubyte * 13)()
    rbuf[0] = REPORT_ID
    if not hid.HidD_GetInputReport(h, rbuf, 13):
        return "GET_REPORT failed (device alive? config mode?)"
    pkt = bytes(rbuf)
    if pkt[1:3] != b"\xaa\x55":
        return "bad framing: %s" % pkt.hex()
    t = pkt[3]
    if t == 5:  # TC_PULL_DATA: real text buffered, no diag this time
        return "text buffered on board (pull it first to see diag)"
    if t != 0xFF:
        return "unexpected type 0x%02X" % t
    d = pkt[4:12]
    role = (d[3] >> 1) & 0xFF
    return ("board=%s profile=%d active_out=%d config_mode=%d type4_seen=%d"
            % (ROLE_NAME.get(role, str(role)), d[0], d[2], d[3] & 1, d[1]))


def main():
    ap = argparse.ArgumentParser(description="Manually send TC_LAYOUT_SELECT (type=4) to the DeskHop.")
    ap.add_argument("profile", type=int, choices=[0, 1], nargs="?",
                    help="0=HOST(P1) 1=LAPTOP(P2); omit with --probe")
    ap.add_argument("--probe", action="store_true",
                    help="only read the board diagnostics, send nothing")
    ap.add_argument("--repeat", type=int, default=1, help="how many times to send (default 1)")
    ap.add_argument("--interval", type=int, default=250, help="ms between sends (default 250)")
    args = ap.parse_args()

    h = open_vendor_hid()
    if not h:
        sys.exit("DeskHop vendor HID not found (VID_1209&PID_C000&MI_02).\n"
                 "Is the board running v0.80.3+ and NOT in config mode?")
    try:
        if args.probe:
            print(probe(h))
            return
        if args.profile is None:
            ap.error("profile is required unless --probe is given")

        before = probe(h)
        print("[before] %s" % before)
        data = bytes([args.profile, 0, 0, 0, 0, 0, 0, 0])
        for i in range(args.repeat):
            ok = send_packet(h, TC_LAYOUT_SELECT, data)
            print("[%d/%d] TC_LAYOUT_SELECT profile=%d -> %s"
                  % (i + 1, args.repeat, args.profile, "SENT OK" if ok else "FAILED"))
            if ok and i < args.repeat - 1:
                time.sleep(args.interval / 1000.0)
        after = probe(h)
        print("[after]  %s" % after)
        if before == after:
            print("NOTE: no change in diagnostics - the packet did NOT reach the "
                  "firmware handler (or the board predates v0.80.8).")
    finally:
        k32.CloseHandle(h)


if __name__ == "__main__":
    main()
