#!/usr/bin/env python3
"""switch_source.py -- DDC/CI 切换显示器输入源 + Windows 拓扑摘除（去 KVM 测试脚本）

对应 docs/side-source-switch-proposal.md 的 M0 实测：
  1. DDC/CI 写 VCP 0x60 切换左屏输入源（1=VGA=主机 / 17=HDMI=笔记本）
  2. SetDisplayConfig 拓扑管理：非归属方收缩输出，把幻影屏从桌面摘除

用法（Windows 10+，仅标准库，两台机器都能跑，无需安装任何东西）:
  python switch_source.py probe            # 只读：显示器 + VCP 0x60 当前值
  python switch_source.py paths            # 只读：活动路径 + 连接器类型（诊断用）
  python switch_source.py sw-hdmi          # 切到 HDMI（笔记本）
  python switch_source.py sw-vga           # 切到 VGA（主机）
  python switch_source.py topo-internal    # INTERNAL+数据库（笔记本会回滚，待查）
  python switch_source.py topo-internal-nodb  # hr=87（拓扑标志必须配数据库）
  python switch_source.py topo-panel       # 只留 LVDS/eDP 内板路径（类型不符则提示）
  python switch_source.py topo-keep-primary  # 路径级只留主显路径（实验性，见下）
  python switch_source.py topo-extend      # 恢复扩展（读保存的布局）
  python switch_source.py topo-external    # 仅外接屏（主机专用，笔记本勿用）

topo-keep-primary 已知问题（2026-10-13）：识别/关联/重映射全部正确，
SUPPLIED|VALIDATE 通过（配置合法），但实际 apply 一律 hr=87（flags 组合
已扫、statusFlags 清零已试）——路径级摘除暂不可用，留档备查。
结论：主机侧摘除用 topo-external；笔记本侧程序化摘除放弃（DDC 死+
拓扑回滚），见实测结论。
  python switch_source.py to-laptop        # 主机跑：切HDMI + 等3s + 摘除(external)
  python switch_source.py to-pc            # 主机跑：切VGA + 等3s + 恢复扩展

实测结论（2026-10-13）:
  [主机]
  - DDC 双向切换 OK：左屏在 HDMI 时写 0x60=1 切回 VGA，再写 17 切回 HDMI
  - 摘除必须用 topo-external（保留右屏真屏、摘掉幻影）；
    topo-internal 在桌面机=留下幻影路、关掉真右屏、显示器自动跳源（勿用）
  - topo-extend 恢复保存布局，显示器自动跳回 VGA（自动源检测实锤）
  [笔记本]
  - DDC 全灭：VCP 读/写在笔记本上对本机两路都失败 → 笔记本侧永不发起 DDC
  - topo-internal+数据库：应用后立即回滚（黑一下恢复），机制未明；
    topo-internal-nodb hr=87（拓扑标志按 MSDN 必须配 DATABASE_CURRENT，单独用非法）
  - topo-panel 报"无内嵌面板路径"→ 本机内板连接器类型非 LVDS/eDP，待 paths 输出
  - **推荐：topo-keep-primary**（路径级只留主显原点路径，不依赖连接器类型）
  - 勿跑 topo-external / to-laptop / to-pc（会摘掉内屏，已加 --yes 护栏）
  [自动源检测（实锤）]
  - 某源信号消失 → 自动跳到有信号的源；信号出现不抢当前活跃源
  - DDC 依赖活动显示路径：拓扑摘除后该侧路径被关，DDC 不可达
    → to-pc 已改为先 topo-extend 恢复路径、再写 DDC
  [新交接协议（拓扑驱动，自动源检测做执行器，DDC 兜底）]
  - 主机交出：to-laptop（DDC 17 + topo-external）✅ 已闭环
  - 主机收回：to-pc（topo-extend 先行 + DDC 1）✅ 已闭环
  - 笔记本交出（待验证）：topo-keep-primary → HDMI 信号消失 → 自动跳 VGA
  - 幻影屏 EDID 名 "Generic PnP Monitor"，VCP 经非活动源（主机侧）可读可写

安全护栏：sw-* 默认只写【非主显示器】且当前 0x60 值在 {1,17} 里的
物理显示器。本机右屏（主显）自己也是 HDMI 输入（0x60=17），靠当前值
过滤拦不住，必须按主/非主身份过滤；--target <子串> 再收窄，--dry-run 预览。
"""

import argparse
import ctypes
import struct
import sys
import time
from ctypes import wintypes

# 输出统一 UTF-8，避免控制台编码乱码
sys.stdout.reconfigure(encoding="utf-8")

VCP_INPUT = 0x60
SRC_VGA = 1     # VGA  -> 主机 (Output A)
SRC_HDMI = 17   # HDMI -> 笔记本 (Output B)

SDC_TOPOLOGY_INTERNAL = 0x1
SDC_TOPOLOGY_CLONE = 0x2
SDC_TOPOLOGY_EXTEND = 0x4
SDC_TOPOLOGY_EXTERNAL = 0x8
SDC_USE_DATABASE_CURRENT = 0x80
SDC_USE_SUPPLIED_DISPLAY_CONFIG = 0x20   # 注意：0x2 是 SDC_TOPOLOGY_CLONE！
SDC_ALLOW_CHANGES = 0x400

QDC_ALL_PATHS = 1
QDC_ONLY_ACTIVE_PATHS = 2
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE = 1
DISPLAYCONFIG_MODE_INFO_TYPE_TARGET = 2

SM_CMONITORS = 80

# DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY
TECH_NAMES = {
    0: "其他", 1: "VGA(HD15)", 5: "DVI", 6: "HDMI", 7: "LVDS(内板)",
    10: "DP外接", 11: "eDP(内板)", 13: "UDI内板", 15: "Miracast",
    0x80000000: "内板标记(0x80000000)",
}
# 笔记本内屏在 API 层的标识：标准 LVDS/eDP + 厂商高位标记（笔记本实测 0x80000000）
EMBEDDED_TECHS = {7, 11, 13, 0x80000000}

user32 = ctypes.windll.user32
dxva2 = ctypes.windll.dxva2

# per-monitor DPI awareness：坐标必须是物理像素
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", wintypes.HANDLE),
                ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]


class REGION2D(ctypes.Structure):
    _fields_ = [("cx", wintypes.UINT), ("cy", wintypes.UINT)]


class PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", wintypes.UINT),
                ("modeInfoIdx", wintypes.UINT), ("statusFlags", wintypes.UINT)]


class PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", wintypes.UINT),
                ("modeInfoIdx", wintypes.UINT),
                ("outputTechnology", wintypes.UINT),
                ("rotation", wintypes.UINT),
                ("scaling", wintypes.UINT),
                ("refreshRate", RATIONAL),
                ("scanLineOrdering", wintypes.UINT),
                ("targetAvailable", wintypes.BOOL),
                ("statusFlags", wintypes.UINT)]


class PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", PATH_SOURCE_INFO),
                ("targetInfo", PATH_TARGET_INFO),
                ("flags", wintypes.UINT)]


class MODE_INFO(ctypes.Structure):
    # 实测 stride=64（主机抓到）：union 有效内容 SOURCE_MODE(20)/TARGET_MODE(44)，
    # 结构尾部有填充到 64。src/tgt 的 modeInfoIdx 直接给出 modes 数组下标。
    _fields_ = [("infoType", wintypes.UINT),
                ("id", wintypes.UINT),
                ("adapterId", LUID),
                ("payload", ctypes.c_ubyte * 40),
                ("statusFlags", wintypes.UINT),
                ("pad", wintypes.UINT)]


class TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("type", wintypes.UINT), ("size", wintypes.UINT),
                ("adapterId", LUID), ("id", wintypes.UINT),
                ("flags", wintypes.UINT),
                ("outputTechnology", wintypes.UINT),
                ("edidManufactureId", wintypes.USHORT),
                ("edidProductCodeId", wintypes.USHORT),
                ("connectorInstance", wintypes.UINT),
                ("monitorFriendlyDeviceName", wintypes.WCHAR * 64)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(RECT), wintypes.LPARAM)

user32.SetDisplayConfig.argtypes = [
    wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
user32.SetDisplayConfig.restype = ctypes.c_long

user32.GetDisplayConfigBufferSizes.argtypes = [
    wintypes.UINT, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
user32.GetDisplayConfigBufferSizes.restype = ctypes.c_long

user32.QueryDisplayConfig.argtypes = [
    wintypes.UINT, ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(PATH_INFO), ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(MODE_INFO), ctypes.POINTER(wintypes.UINT)]
user32.QueryDisplayConfig.restype = ctypes.c_long

user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]
user32.DisplayConfigGetDeviceInfo.restype = ctypes.c_long

dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
    wintypes.HANDLE, wintypes.BYTE,
    ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(wintypes.UINT)]
dxva2.GetVCPFeatureAndVCPFeatureReply.restype = wintypes.BOOL

dxva2.SetVCPFeature.argtypes = [wintypes.HANDLE, wintypes.BYTE, wintypes.DWORD]
dxva2.SetVCPFeature.restype = wintypes.BOOL

dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD)]
dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL

dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL

dxva2.DestroyPhysicalMonitors.argtypes = [
    wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
dxva2.DestroyPhysicalMonitors.restype = wintypes.BOOL


def each_logical_monitor():
    """枚举逻辑显示器，产出 (primary_flag, MONITORINFOEXW, [PHYSICAL_MONITOR])。"""
    found = []

    def _cb(hmon, hdc, lprect, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            count = wintypes.DWORD(0)
            pms = []
            if dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)):
                if count.value:
                    arr = (PHYSICAL_MONITOR * count.value)()
                    if dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
                        pms = list(arr)
            found.append((bool(mi.dwFlags & 1), mi, pms))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    return found


def release(pms):
    if pms:
        dxva2.DestroyPhysicalMonitors(len(pms),
                                      (PHYSICAL_MONITOR * len(pms))(*pms))


def read_vcp(hpm, code=VCP_INPUT):
    t, cur, mx = wintypes.UINT(), wintypes.UINT(), wintypes.UINT()
    if dxva2.GetVCPFeatureAndVCPFeatureReply(hpm, code, ctypes.byref(t),
                                             ctypes.byref(cur), ctypes.byref(mx)):
        return cur.value, mx.value
    return None, None


def write_vcp(hpm, value, code=VCP_INPUT):
    return bool(dxva2.SetVCPFeature(hpm, code, value))


def action_probe():
    print("=" * 72)
    print("显示器探测（物理像素坐标）")
    print("=" * 72)
    for is_primary, mi, pms in each_logical_monitor():
        r = mi.rcMonitor
        primary = "[主显]" if is_primary else "[扩展]"
        print(f"\n{primary} {mi.szDevice}  rect=({r.left},{r.top})-"
              f"({r.right},{r.bottom})  {r.right - r.left}x{r.bottom - r.top}")
        if not pms:
            print("  (无物理显示器句柄)")
            continue
        for pm in pms:
            desc = pm.szPhysicalMonitorDescription or "(未命名)"
            cur, mx = read_vcp(pm.hPhysicalMonitor)
            if cur is None:
                print(f"  物理 \"{desc}\"  VCP 0x60 读取失败")
            else:
                tag = {SRC_VGA: "  <- VGA/主机", SRC_HDMI: "  <- HDMI/笔记本"}.get(cur, "")
                print(f"  物理 \"{desc}\"  VCP 0x60 当前={cur} max={mx}{tag}")
        release(pms)
    n = user32.GetSystemMetrics(SM_CMONITORS)
    print(f"\n逻辑显示器总数 = {n}"
          f"   （2 = 有幻影屏在场；1 = 已摘除/单源）")


def query_active_paths():
    """返回 (paths, modes) —— QDC_ONLY_ACTIVE_PATHS。"""
    n_path, n_mode = wintypes.UINT(), wintypes.UINT()
    hr = user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS,
                                            ctypes.byref(n_path), ctypes.byref(n_mode))
    if hr != 0:
        print(f"GetDisplayConfigBufferSizes 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return None, None
    paths = (PATH_INFO * n_path.value)()
    modes = (MODE_INFO * n_mode.value)()
    hr = user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS,
                                   ctypes.byref(n_path), paths,
                                   ctypes.byref(n_mode), modes, None)
    if hr != 0:
        print(f"QueryDisplayConfig 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return None, None
    return list(paths)[:n_path.value], list(modes)[:n_mode.value]


def target_friendly_name(path):
    """已知问题：本机 DisplayConfigGetDeviceInfo 一律返回 87，暂不使用。
    路径身份判定只依赖 outputTechnology / 源模式位置，不影响功能。"""
    return ""


def primary_origin():
    """主显示器（笔记本=内屏 / 主机=右屏）的桌面原点。"""
    for is_primary, mi, pms in each_logical_monitor():
        r = mi.rcMonitor
        release(pms)
        if is_primary:
            return (r.left, r.top)
    return None


def source_mode_pos(mode):
    """解码 MODE_INFO 里的 DISPLAYCONFIG_SOURCE_MODE.position。
    SOURCE_MODE = { width@0, height@4, pixelFormat@8, POINTL position@12..20 }"""
    x, y = struct.unpack_from("<ii", bytes(mode.payload), 12)
    return (x, y)


def source_mode_of(paths, modes, path):
    """按 sourceInfo.modeInfoIdx 找到路径对应的 SOURCE 模式条目。"""
    idx = path.sourceInfo.modeInfoIdx
    if idx < len(modes):
        m = modes[idx]
        if m.infoType == DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE:
            return m
    # 兜底：按 id 匹配
    for m in modes:
        if m.infoType == DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE and m.id == path.sourceInfo.id:
            return m
    return None


def apply_paths(keep, modes, label):
    """路径级 SetDisplayConfig：只提交 keep 里的路径（未提交的=关闭）。
    关键：截断后的 modes 数组下标变了，必须把路径里的 modeInfoIdx 重映射。"""
    kmodes = list(modes)  # 先整体拷贝，再筛
    keep_src = {p.sourceInfo.id for p in keep}
    keep_tgt = {p.targetInfo.id for p in keep}
    kmodes = [m for m in kmodes
              if (m.infoType == DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE and m.id in keep_src)
              or (m.infoType == DISPLAYCONFIG_MODE_INFO_TYPE_TARGET and m.id in keep_tgt)]
    # 重映射：modeInfoIdx → 新数组下标
    new_idx = {(m.infoType, m.id): i for i, m in enumerate(kmodes)}
    kpaths = []
    for p in keep:
        p.sourceInfo.modeInfoIdx = new_idx[(DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE, p.sourceInfo.id)]
        p.targetInfo.modeInfoIdx = new_idx[(DISPLAYCONFIG_MODE_INFO_TYPE_TARGET, p.targetInfo.id)]
        kpaths.append(p)
    parr = (PATH_INFO * len(kpaths))(*kpaths)
    marr = (MODE_INFO * len(kmodes))(*kmodes)
    hr = user32.SetDisplayConfig(len(kpaths), parr, len(kmodes), marr,
                                 SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_ALLOW_CHANGES)
    if hr == 0:
        print(f"{label}: OK")
    else:
        print(f"{label}: 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return hr
    print("等 2 秒 ...")
    time.sleep(2)
    action_probe()
    return hr


def action_topo_keep_primary():
    """路径级摘除（通用）：只保留主显示器所在路径，其余（幻影）关闭。
    笔记本=只留内屏；主机=只留右屏。按主显原点匹配，不依赖连接器类型。"""
    paths, modes = query_active_paths()
    if paths is None:
        return
    prim = primary_origin()
    if prim is None:
        print("找不到主显示器")
        return
    keep, drop = [], []
    for p in paths:
        m = source_mode_of(paths, modes, p)
        pos = source_mode_pos(m) if m else None
        (keep if pos == prim else drop).append((p, pos))
    if not keep:
        print(f"没有源模式位置=主显原点 {prim} 的路径，无法判定")
        return
    print(f"主显原点={prim}")
    for p, pos in keep:
        print(f"  保留: src_id={p.sourceInfo.id} 位置={pos} "
              f"连接器={TECH_NAMES.get(p.targetInfo.outputTechnology, p.targetInfo.outputTechnology)}")
    for p, pos in drop:
        print(f"  丢弃: src_id={p.sourceInfo.id} 位置={pos} "
              f"连接器={TECH_NAMES.get(p.targetInfo.outputTechnology, p.targetInfo.outputTechnology)}")
    apply_paths([p for p, _ in keep], modes, "topo-keep-primary")


def action_paths():
    paths, modes = query_active_paths()
    if paths is None:
        return
    prim = primary_origin()
    print("=" * 72)
    print(f"活动路径（QDC_ONLY_ACTIVE_PATHS）  主显原点={prim}")
    print("=" * 72)
    for p in paths:
        m = source_mode_of(paths, modes, p)
        pos = source_mode_pos(m) if m else None
        tech = p.targetInfo.outputTechnology
        tname = TECH_NAMES.get(tech, f"tech{tech}")
        mark = "  <-主显路径" if pos == prim else ""
        print(f"  src_id={p.sourceInfo.id}  tgt_id={p.targetInfo.id}  "
              f"连接器={tname}  源位置={pos}{mark}")
    print(f"\n共 {len(paths)} 条路径")
    print("判读：内板应显示 LVDS/eDP(内板)；左屏幻影=VGA 或 DVI/HDMI；右屏=DP/HDMI")


def action_topo_panel():
    """路径级摘除（按连接器类型）：只保留内嵌面板（LVDS/eDP）路径。
    若本机内板连接器类型不在 {7,11,13}（笔记本实测可能如此），
    改用 topo-keep-primary（按主显原点匹配，通用）。"""
    paths, modes = query_active_paths()
    if paths is None:
        return
    keep = [p for p in paths if p.targetInfo.outputTechnology in EMBEDDED_TECHS]
    if not keep:
        print("没有找到内嵌面板路径（内板连接器类型非 LVDS/eDP？）\n"
              "改用 topo-keep-primary（按主显原点匹配，不依赖连接器类型）")
        return
    print("保留路径:")
    for p in keep:
        print(f"  src_id={p.sourceInfo.id}  连接器={TECH_NAMES.get(p.targetInfo.outputTechnology)}")
    print("丢弃路径:")
    for p in paths:
        if p not in keep:
            print(f"  src_id={p.sourceInfo.id}  连接器={TECH_NAMES.get(p.targetInfo.outputTechnology)}")
    apply_paths(keep, modes, "topo-panel")


def set_source(value, label, target=None, dry_run=False):
    """写 VCP 0x60。
    护栏：默认只写【非主显示器】且当前 0x60 值在 {1,17} 里的物理显示器。
    本机右屏（主显）自己也是 HDMI 输入（0x60=17），按当前值过滤拦不住它，
    必须按主/非主身份过滤；--target 可再按名称收窄。
    """
    print(f"--> 切换输入源 -> {value} ({label})")
    written = 0
    for is_primary, mi, pms in each_logical_monitor():
        if is_primary and target is None:
            print("    跳过主显示器（护栏：非 --target 不碰主显）")
            continue
        for pm in pms:
            desc = pm.szPhysicalMonitorDescription or "(未命名)"
            cur, _ = read_vcp(pm.hPhysicalMonitor)
            if cur is None:
                print(f"    跳过 \"{desc}\"（VCP 读取失败）")
                continue
            if target is not None:
                if target.lower() not in desc.lower():
                    print(f"    跳过 \"{desc}\" 当前={cur}（名称不匹配 --target）")
                    continue
            elif cur not in (SRC_VGA, SRC_HDMI):
                print(f"    跳过 \"{desc}\" 当前={cur}（不在 {{1,17}}，护栏拦下）")
                continue
            if dry_run:
                print(f"    [dry-run] 将写 \"{desc}\" {cur} -> {value}")
                continue
            if write_vcp(pm.hPhysicalMonitor, value):
                written += 1
                print(f"    已写 \"{desc}\" {cur} -> {value}")
            else:
                print(f"    写入失败 \"{desc}\"")
        release(pms)
    if dry_run:
        print("dry-run，未实际写入")
        return
    print(f"共写 {written} 台，等 2-3 秒切源生效")
    if written == 0:
        print("提示：写 0 台常见原因——①该侧刚做过拓扑摘除，路径已关，"
              "DDC 不可达（先 topo-extend）；②笔记本侧 DDC 通道实测不可用"
              "（VCP 读失败），切换请改在主机上发起")


def set_topology(flags, label):
    hr = user32.SetDisplayConfig(0, None, 0, None, flags)
    if hr == 0:
        print(f"拓扑 -> {label}: OK")
    else:
        print(f"拓扑 -> {label}: 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    return hr


def set_topology_and_probe(flags, label):
    hr = set_topology(flags, label)
    if hr == 0:
        print("等 2 秒 ...")
        time.sleep(2)
        action_probe()
    return hr


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=[
        "probe", "paths", "sw-hdmi", "sw-vga",
        "topo-internal", "topo-internal-nodb", "topo-panel", "topo-keep-primary",
        "topo-extend", "topo-external",
        "to-laptop", "to-pc"], nargs="?", default="probe")
    ap.add_argument("--target", default=None,
                    help="仅写描述匹配此子串的显示器（护栏收窄用）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只预览将写哪些显示器，不实际写")
    ap.add_argument("--yes", action="store_true",
                    help="跳过主机专用动作的安全确认")
    args = ap.parse_args()

    print(f"=== switch_source.py : {args.action} ===")
    a = args.action

    # 主机专用动作护栏：topo-external 保留外接屏，在笔记本上会摘掉内屏
    if a in ("topo-external", "to-laptop", "to-pc") and not args.yes:
        print("⚠ 此动作为【主机专用】（topo-external 保留外接屏/右屏）。")
        print("  在笔记本上执行会把内屏摘掉。确在主机上执行请追加 --yes")
        return

    if a == "probe":
        action_probe()
    elif a == "paths":
        action_paths()
    elif a == "sw-hdmi":
        set_source(SRC_HDMI, "HDMI/笔记本", args.target, args.dry_run)
    elif a == "sw-vga":
        set_source(SRC_VGA, "VGA/主机", args.target, args.dry_run)
    elif a == "topo-internal":
        # 实测：主机上=留下幻影路关掉右屏（勿用）；笔记本上=闪一下即回滚（待查）
        set_topology_and_probe(SDC_TOPOLOGY_INTERNAL | SDC_USE_DATABASE_CURRENT,
                               "单屏-INTERNAL+数据库")
    elif a == "topo-internal-nodb":
        set_topology_and_probe(SDC_TOPOLOGY_INTERNAL, "单屏-INTERNAL无数据库")
    elif a == "topo-panel":
        action_topo_panel()
    elif a == "topo-keep-primary":
        action_topo_keep_primary()
    elif a == "topo-extend":
        set_topology_and_probe(SDC_TOPOLOGY_EXTEND | SDC_USE_DATABASE_CURRENT,
                               "扩展（恢复保存布局）")
    elif a == "topo-external":
        set_topology_and_probe(SDC_TOPOLOGY_EXTERNAL | SDC_USE_DATABASE_CURRENT,
                               "单屏-仅外接（主机摘除用）")
    elif a == "to-laptop":
        set_source(SRC_HDMI, "HDMI/笔记本", args.target, args.dry_run)
        print("等 3 秒切源 ...")
        time.sleep(3)
        # 实测 2026-10-13：桌面机上 INTERNAL 会把幻影路留下、把真主显关掉，
        # 主机侧摘除必须用 EXTERNAL（实测保留右屏真屏）
        set_topology(SDC_TOPOLOGY_EXTERNAL | SDC_USE_DATABASE_CURRENT,
                     "单屏-仅右屏（摘除幻影左屏）")
        print(f"SM_CMONITORS = {user32.GetSystemMetrics(SM_CMONITORS)}")
    elif a == "to-pc":
        # 主机收回。实测教训：拓扑摘除后 VGA 路径被关，DDC 无路可达
        # → 必须先 topo-extend 恢复路径（幻影路复活），再写 DDC 切源
        set_topology(SDC_TOPOLOGY_EXTEND | SDC_USE_DATABASE_CURRENT,
                     "扩展（先恢复 VGA 路径）")
        print("等 2 秒等路径就绪 ...")
        time.sleep(2)
        set_source(SRC_VGA, "VGA/主机", args.target, args.dry_run)
        print(f"SM_CMONITORS = {user32.GetSystemMetrics(SM_CMONITORS)}")


if __name__ == "__main__":
    main()
