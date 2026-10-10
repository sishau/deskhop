"""DeskHop 显示器几何探测 —— 在主机和笔记本上各跑一次。

列出 Windows 眼中每台显示器的：
  - 矩形位置 (虚拟桌面坐标) —— 判断副显是否与主显齐边、有无垂直偏移
  - 当前分辨率/刷新率 —— 判断 KVM 盒伪造 EDID 后 Windows 实际给的分辨率
  - 是否主显示器

v4 平面模型假设：副显 1920x1080、与主显齐边（底部对齐）。
若探测结果与此不符，脉冲进门失败（光标被钳回主显）就有了实锤：
  - 分辨率不同 → 在 webconfig 里把 side_width_px / side_height_px 改成探测值即可
  - 有垂直偏移 → 固件停泊公式需按偏移调整（把输出发给 AI）

依赖：仅 Python 标准库。Windows 10+。
"""

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

# per-monitor DPI awareness：坐标必须是物理像素
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


class POINTL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32),
                ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD),
                ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD),
                ("dmPosition", POINTL),
                ("dmDisplayOrientation", wintypes.DWORD),
                ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", wintypes.SHORT),
                ("dmDuplex", wintypes.SHORT),
                ("dmYResolution", wintypes.SHORT),
                ("dmTTOption", wintypes.SHORT),
                ("dmCollate", wintypes.SHORT),
                ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD),
                ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD),
                ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD),
                ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD),
                ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD),
                ("dmPanningHeight", wintypes.DWORD)]


ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
MONITORINFOF_PRIMARY = 1

monitors = []


def _callback(hmon, hdc, lprect):
    mi = MONITORINFOEXW()
    mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        monitors.append(mi)
    return True


CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR,
                        wintypes.HDC, ctypes.POINTER(RECT))

user32.EnumDisplayMonitors(None, None, CB(_callback), 0)

print("=" * 72)
print("显示器几何（Windows 虚拟桌面坐标，物理像素）")
print("=" * 72)

for mi in monitors:
    r = mi.rcMonitor
    w, h = r.right - r.left, r.bottom - r.top
    primary = "[主显]" if (mi.dwFlags & MONITORINFOF_PRIMARY) else "[扩展]"

    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    mode_ok = user32.EnumDisplaySettingsW(mi.szDevice, ENUM_CURRENT_SETTINGS,
                                          ctypes.byref(dm))
    if mode_ok:
        res = f"{dm.dmPelsWidth}x{dm.dmPelsHeight} @{dm.dmDisplayFrequency}Hz"
        pos = f"dmPosition=({dm.dmPosition.x},{dm.dmPosition.y})"
    else:
        res, pos = "??", "??"

    print(f"\n{primary} {mi.szDevice}")
    print(f"  矩形: ({r.left},{r.top}) - ({r.right},{r.bottom})   {w}x{h}")
    print(f"  当前模式: {res}   {pos}")

vx = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
vy = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
vw = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
vh = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
print("\n" + "-" * 72)
print(f"虚拟桌面整体: ({vx},{vy}) - ({vx+vw},{vy+vh})   {vw}x{vh}")
print()
print("判读（对照 v4 假设 副显1920x1080、与主显齐边）:")
print("  - 副显矩形应为 (x=-1920, y=0) 起、1920x1080（主显左侧、垂直齐边）")
print("  - 宽高 != 1920x1080  → KVM 盒 EDID 分辨率不同, 改 webconfig side_*_px")
print("  - y 不为 0 / 高度不足 → 有垂直偏移, 底部脉冲会撞墙（把输出发给 AI）")
