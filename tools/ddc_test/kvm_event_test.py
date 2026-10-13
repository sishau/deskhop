#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kvm_event_test.py -- 验证 WM_DISPLAYCHANGE 事件驱动是否可靠

原理：
  建一个隐藏窗口收广播消息。KVM 切换时 HPD 变化 → Windows 广播
  WM_DISPLAYCHANGE (0x007E)。同时跑一个 1s 对账轮询（WM_TIMER），
  如果 poll 抓到了 count 变化而事件没来 → 事件丢失实锤。

  wparam=色深  lparam=低字宽/高字高（注意：不带显示器数量！
  所以处理器里必须自己重新枚举 count——与最终 Rust 实现一致）

用法：python kvm_event_test.py [秒数]     （缺省 Ctrl+C 停止）
输出：每个事件/对账变化一行，格式 [时:分:秒.毫秒] 类型 内容
"""
import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
# lParam 回调里是无符号大整数，必须声明 argtypes 否则 OverflowError
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]

WM_DISPLAYCHANGE = 0x007E
WM_DEVICECHANGE = 0x0219
WM_TIMER = 0x0113
WM_DESTROY = 0x0002

TIMER_ID = 1
TIMER_MS = 1000  # 对账轮询周期

# ----------------- Win32 FFI -----------------

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", POINT)]

class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.WINFUNCTYPE(
                    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                    wintypes.WPARAM, wintypes.LPARAM)),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HINSTANCE),
                ("hCursor", wintypes.HINSTANCE), ("hbrBackground", wintypes.HINSTANCE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(POINT), wintypes.LPARAM)


def count_monitors():
    """只数数量，不做任何 VCP/EDID 查询（保持与最终 Rust watcher 同构）。"""
    n = [0]

    def cb(_hm, _hd, _rc, _lp):
        n[0] += 1
        return True

    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(cb), 0)
    return n[0]


def ts():
    return time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000)


def log(line):
    print(f"[{ts()}] {line}", flush=True)


# ----------------- 窗口与消息循环 -----------------

last_count = None
event_hits = 0   # 事件抓到的 count 变化数
poll_hits = 0    # poll 兜底抓到的变化数


def on_count_change(source, new_count):
    global last_count, event_hits, poll_hits
    last_count = new_count
    if source == "EVENT":
        event_hits += 1
    else:
        poll_hits += 1
    log(f"{source}: 显示器数量 -> {new_count}")


def wnd_proc(hwnd, msg, wp, lp):
    global last_count
    if msg == WM_DISPLAYCHANGE:
        # 与最终实现一致：事件不带 count，收到后重新枚举
        new_count = count_monitors()
        log(f"EVENT: WM_DISPLAYCHANGE wparam={wp} lparam=0x{lp & 0xFFFFFFFF:08X} "
            f"count={new_count}")
        if last_count is not None and new_count != last_count:
            on_count_change("EVENT", new_count)
        else:
            log(f"EVENT: count 未变（{new_count}），可能是分辨率/色深变化")
    elif msg == WM_DEVICECHANGE:
        log(f"EVENT: WM_DEVICECHANGE wparam=0x{wp:04X}")
    elif msg == WM_TIMER:
        new_count = count_monitors()
        if last_count is not None and new_count != last_count:
            log("POLL: 事件未到、对账轮询抓到变化（事件丢失！）")
            on_count_change("POLL", new_count)
        else:
            last_count = new_count
    elif msg == WM_DESTROY:
        user32.PostQuitMessage(0)
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
proc_ref = WNDPROC(wnd_proc)  # 防回收


def main():
    global last_count
    deadline = None
    if len(sys.argv) > 1 and sys.argv[1].replace(".", "").isdigit():
        deadline = time.time() + float(sys.argv[1])

    print("=== kvm_event_test.py ===")
    print(f"事件监听：WM_DISPLAYCHANGE / WM_DEVICECHANGE；对账轮询 {TIMER_MS}ms")
    print("请按 KVM 按钮：切走 → 3s → 切回 → 3s → ...（多来几轮）\n")

    last_count = count_monitors()
    log(f"基线：显示器数量 = {last_count}")

    hinst = ctypes.windll.kernel32.GetModuleHandleW(0)
    cls = "KvmEventTest"
    wc = WNDCLASSW()
    wc.lpfnWndProc = proc_ref
    wc.lpszClassName = cls
    wc.hInstance = hinst
    if not user32.RegisterClassW(ctypes.byref(wc)):
        log("RegisterClassW 失败")
        return

    hwnd = user32.CreateWindowExW(
        0, cls, "kvm_event_test", 0, 0, 0, 0, 0, 0, 0, hinst, 0)
    if not hwnd:
        log("CreateWindowExW 失败")
        return
    user32.SetTimer(hwnd, TIMER_ID, TIMER_MS, 0)

    msg = MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
            if deadline and time.time() >= deadline:
                break
    except KeyboardInterrupt:
        pass

    print("\n" + "=" * 60)
    print(f"统计：事件抓到变化 {event_hits} 次，poll 兜底抓到 {poll_hits} 次")
    if poll_hits == 0:
        print("结论：事件驱动可靠，事件无一丢失 ✓")
    else:
        print("结论：存在事件丢失，需要更强的轮询兜底 ✗")


if __name__ == "__main__":
    main()
