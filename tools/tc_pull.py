# tc_pull.py - DeskHop text channel, pull only (firmware v0.80.3+).
# Python 3 stdlib only, no install. Usage: python tc_pull.py
# Pulled text goes to the clipboard and stdout.
#
# ctypes pitfall (this file's history): HDEVINFO / HANDLE are POINTER-SIZED
# on x64. Without explicit restype/argtypes ctypes defaults to 32-bit c_int,
# truncates the handle, and SetupDiEnumDeviceInterfaces fails on index 0
# regardless of whether the device is present.
import ctypes
import sys

u32, k32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
hid, setup = ctypes.WinDLL("hid.dll"), ctypes.WinDLL("setupapi.dll")

hid.HidD_SetOutputReport.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
hid.HidD_SetOutputReport.restype = ctypes.c_ubyte
hid.HidD_GetInputReport.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
hid.HidD_GetInputReport.restype = ctypes.c_ubyte

k32.CreateFileW.restype = ctypes.c_void_p
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalAlloc.argtypes = [ctypes.c_uint32, ctypes.c_size_t]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]

u32.SetClipboardData.restype = ctypes.c_void_p
u32.SetClipboardData.argtypes = [ctypes.c_uint32, ctypes.c_void_p]

setup.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setup.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                   ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
setup.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

INVALID = 18446744073709551615  # (unsigned)-1 on x64


class GUID(ctypes.Structure):
    _fields_ = [("d0", ctypes.c_uint32), ("d1", ctypes.c_uint16),
                ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint8 * 8)]


class IF(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("g", GUID),
                ("f", ctypes.c_uint32), ("r", ctypes.c_size_t)]


# open the vendor collection: VID_1209&PID_C000, interface MI_02
g = GUID(); hid.HidD_GetHidGuid(ctypes.byref(g))
info = setup.SetupDiGetClassDevsW(ctypes.byref(g), None, None, 0x12)  # PRESENT|DEVICEINTERFACE
if not info or info == INVALID:
    sys.exit("SetupDiGetClassDevs failed")
h = 0
for i in range(64):
    d = IF(); d.cb = ctypes.sizeof(d)
    if not setup.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(g), i, ctypes.byref(d)): break
    n = ctypes.c_uint32()
    setup.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(d), None, 0, ctypes.byref(n), None)
    if not n.value: continue
    b = ctypes.create_string_buffer(n.value)
    b[0:4] = (8).to_bytes(4, "little")                # cbSize = 8 on x64
    if not setup.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(d), b, n, None, None): continue
    p = ctypes.wstring_at(ctypes.addressof(b) + 4).lower()
    if "vid_1209&pid_c000" in p and "mi_02" in p:
        h = k32.CreateFileW(p, 0xC0000000, 3, None, 3, 0, None)  # RW, shared, OPEN_EXISTING
        break
setup.SetupDiDestroyDeviceInfoList(info)
if not h or h == INVALID:
    sys.exit("DeskHop vendor HID not found (VID_1209&PID_C000&MI_02).\n"
             "Is the board running v0.80.3 and NOT in config mode?")

# PULL_REQ (type 3, zero data, zero checksum) restarts the read side
hid.HidD_SetOutputReport(h, bytes([6, 0xAA, 0x55, 3] + [0] * 9), 13)

# GET_REPORT loop: [6][0xAA][0x55][type][d0..d7][xor(d0..d7)]
# type 5 = data, data[7] = 0x80|n marks the last chunk with n bytes; 0xFF = empty
out = bytearray()
rb = (ctypes.c_ubyte * 13)(); rb[0] = 6
for _ in range(1300):
    if not hid.HidD_GetInputReport(h, rb, 13):
        sys.exit("GET_REPORT failed (re-run to retry)")
    p = bytes(rb)
    if p[3] == 0xFF: break
    c = 0
    for x in p[4:12]: c ^= x
    if p[1:3] != b"\xaa\x55" or p[3] != 5 or c != p[12]:
        sys.exit("bad packet from device (framing/type/checksum)")
    fl = p[11]
    out += p[4:4 + ((fl & 0x7F) if fl & 0x80 else 7)]
    if fl & 0x80: break

if not out:
    sys.exit("nothing buffered on this board's Pico")
text = out.decode("utf-8", "replace")

if u32.OpenClipboard(0):
    u32.EmptyClipboard()
    d = text.encode("utf-16-le") + b"\x00\x00"
    gh = k32.GlobalAlloc(2, len(d)); gp = k32.GlobalLock(gh)
    ctypes.memmove(gp, d, len(d)); k32.GlobalUnlock(gh)
    u32.SetClipboardData(13, gh)
    u32.CloseClipboard()
try: sys.stdout.reconfigure(errors="replace")
except Exception: pass
print(text)
