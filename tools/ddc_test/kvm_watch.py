#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kvm_watch.py -- KVM 状态观察器：找出"按下 KVM 按钮时主机侧哪个可观测量会变"

背景（2026-10-13）：
  - KVM 盒 EDID 常驻 + HPD 恒保持 → 显示器数量恒为 2，数量本身不是状态比特
  - 候选可观测量（本脚本全部采集，自动 diff）：
      ① VCP 0x60 读结果（值/最大值/成功与否）——KVM 若只给活动源接 DDC 线，
         非活动态读取会失败，这就是我们要的比特
      ② EDID 名（活动源可能拿真 EDID，非活动源拿 KVM 缓存）
      ③ 显示器拓扑（数量/位置/主显标志/刷新率）
  - 若任何观测量随按钮翻转 → 常驻工具 1Hz 轮询它 → 自动发 profile 切换命令给 Pico

用法：
  python kvm_watch.py            # 观察模式，Ctrl+C 停止
  python kvm_watch.py 5          # 只观察 5 秒后自动退出并打印摘要

输出：状态变化时打印时间戳 + 变化前后 diff；结束时打印观测到的所有稳定状态。
"""
import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
dxva2 = ctypes.windll.dxva2

# ------------------------- 显示器枚举 -------------------------

class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]

class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", wintypes.HANDLE),
                ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]


class _DM_UNION(ctypes.Union):
    _fields_ = [("dmPosition", wintypes.POINT),
                ("dmDisplayOrientation", wintypes.DWORD),
                ("dmDisplayFixedOutput", wintypes.DWORD)]


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("u", _DM_UNION),
        # 注意：DEVMODEW 这五个字段是 SHORT（DEVMODEA 才是 BYTE/DWORD 混排）
        ("dmColorMode", wintypes.SHORT), ("dmDuplex", wintypes.SHORT),
        ("dmYResolution", wintypes.SHORT), ("dmTTOption", wintypes.SHORT),
        ("dmCollate", wintypes.SHORT),
        # 实测（2026-10-13 raw dump）：真实 DEVMODEW 的 formName 在 @102，
        # SHORT 块(止于@94)后有 8 字节保留区；用 BYTE×8 避免 ctypes 插对齐垫
        # （dmSize 回写 188 恰好截止 dmDisplayFrequency@184）
        ("dmReserved", ctypes.c_ubyte * 8),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
    ]

MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(RECT), wintypes.LPARAM)

# ------------------------- VCP 读取 -------------------------

dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
    wintypes.HANDLE, wintypes.BYTE,
    ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(wintypes.UINT)]
dxva2.DestroyPhysicalMonitors.argtypes = [
    wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]

VCP_INPUT_SOURCE = 0x60


def read_vcp(hpm, code=VCP_INPUT_SOURCE):
    t, cur, mx = wintypes.UINT(), wintypes.UINT(), wintypes.UINT()
    if dxva2.GetVCPFeatureAndVCPFeatureReply(
            hpm, code, ctypes.byref(t), ctypes.byref(cur), ctypes.byref(mx)):
        return cur.value
    return None


def snapshot():
    """采集一帧全量状态：拓扑 + EDID 名 + 每台物理屏的 VCP 0x60。"""
    frames = []

    def cb(hMon, _hdc, lprc, _lp):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hMon, ctypes.byref(mi)):
            return True
        r = lprc.contents
        primary = bool(mi.dwFlags & 1)
        entry = {
            "dev": mi.szDevice,
            "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
            "primary": primary,
            "freq": None,
            "vcp": [],
        }
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if user32.EnumDisplaySettingsW(mi.szDevice, 0xFFFFFFFF, ctypes.byref(dm)):  # ENUM_CURRENT_SETTINGS
            entry["freq"] = dm.dmDisplayFrequency
        count = wintypes.DWORD()
        if dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hMon, ctypes.byref(count)) and count.value:
            pms = (PHYSICAL_MONITOR * count.value)()
            if dxva2.GetPhysicalMonitorsFromHMONITOR(hMon, count.value, pms):
                for pm in pms:
                    name = pm.szPhysicalMonitorDescription or "?"
                    v = read_vcp(pm.hPhysicalMonitor)
                    entry["vcp"].append((name, v))
                dxva2.DestroyPhysicalMonitors(count.value, pms)
        frames.append(entry)
        return True

    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(cb), 0)
    return frames


def fmt_state(frames):
    lines = [f"逻辑屏数={len(frames)}"]
    for f in frames:
        x, y, w, h = f["rect"]
        prim = "[主显]" if f["primary"] else "[扩展]"
        freq = f"{f['freq']}Hz" if f["freq"] else "?Hz"
        lines.append(f"  {prim} {f['dev']} ({x},{y}) {w}x{h} @{freq}")
        for name, v in f["vcp"]:
            vv = str(v) if v is not None else "读失败"
            lines.append(f"      VCP0x60={vv}  \"{name}\"")
    return "\n".join(lines)


def state_key(frames):
    """可 diff 的状态指纹（含 VCP 值与读失败状态）。"""
    parts = []
    for f in sorted(frames, key=lambda e: e["rect"]):
        vs = "|".join(f"{n}:{v}" for n, v in f["vcp"])
        parts.append(f"{f['dev']}:{f['rect']}:{int(f['primary'])}:{f['freq']}:{vs}")
    return ";".join(parts)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    deadline = time.time() + float(arg) if arg and arg.replace(".", "").isdigit() else None

    print("=== kvm_watch.py : 观察模式（500ms/拍）===")
    print("请在此期间按 KVM 按钮：切走 → 停 3 秒 → 切回 → 停 3 秒 ...")
    print("（每次状态变化会打印 diff；Ctrl+C 结束）\n")

    states = {}          # 指纹 -> 出现次数
    last_key = None
    last_print = ""
    n = 0
    try:
        while True:
            fr = snapshot()
            key = state_key(fr)
            states[key] = states.get(key, 0) + 1
            n += 1
            if key != last_key:
                ts = time.strftime("%H:%M:%S")
                print(f"--- 状态变化 @ {ts} ---")
                print(fmt_state(fr))
                print()
                last_key = key
            if fmt_state(fr) != last_print and last_key is None:
                last_print = fmt_state(fr)
            if deadline and time.time() >= deadline:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    print("=" * 60)
    print(f"观察结束：共 {n} 拍，出现 {len(states)} 种稳定状态")
    for i, (key, cnt) in enumerate(states.items()):
        print(f"\n[状态 {i}] 出现 {cnt} 拍")
    print("\n判读指引：")
    print("  - 若只有 1 种状态 → 数量/EDID/VCP 全部不变，此路不通")
    print("  - 若 VCP 读失败↔成功翻转 → 这就是状态比特（首选，接线到 profile 自动切换）")
    print("  - 若 EDID 名/刷新率翻转 → 备选比特")


if __name__ == "__main__":
    main()
