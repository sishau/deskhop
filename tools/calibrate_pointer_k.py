"""DeskHop 指针倍率 k 标定脚本 —— 两台机器各跑一次。

测量 Windows 指针管道的 k（光标像素位移 / 注入的鼠标 count）：

  1. 检查"提高指针精确度"(EPP)：开启则拒绝标定 —— EPP 下 k 随速度变化，
     标量存不下，且 SendInput 时序与真实 1kHz 设备流不同，测得值不可迁移。
  2. SendInput 注入已知相对 count，GetCursorPos 读回物理像素位移。
  3. 双轴、双方向、三种幅度各测一轮；幅度间偏差 > 2% 视为存在非线性，拒绝给值
     （EPP 的兜底检测，防止注册表状态与实际行为不符）。

注意：
  - 标定期间不要碰物理鼠标（它的增量会混入测量）。
  - 脚本已声明 per-monitor DPI awareness：笔记本 150% 缩放下读到的仍是物理像素。
  - 注入总量按 k<=3 预留了屏幕边距（1920x1080 居中起测），不会触发边缘钳制。

结果：打印各轴 k 值（4 位小数）与千分比整数，按提示写入固件配置。

依赖：仅 Python 标准库（ctypes / winreg）。Windows 10+。
"""

import ctypes
import sys
import winreg

# ---------------------------------------------------------------- DPI awareness
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

user32 = ctypes.windll.user32

ULONG_PTR = ctypes.c_size_t


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]


class _INPUTU(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTU)]


INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000


def cursor_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def send_rel(dx, dy):
    inp = INPUT(INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, 0)
    ok = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if ok != 1:
        sys.exit("SendInput failed (blocked by UIPI or security software?)")


def park_center():
    """绝对放置到主显示器中心，保证四个方向都有足够边距。"""
    inp = INPUT(INPUT_MOUSE)
    inp.mi = MOUSEINPUT(32767, 32767, 0,
                        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def epp_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Mouse") as k:
            val, _ = winreg.QueryValueEx(k, "MouseSpeed")
            return str(val) == "1"
    except FileNotFoundError:
        return False


def read_sensitivity():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Mouse") as k:
            val, _ = winreg.QueryValueEx(k, "MouseSensitivity")
            return int(val)
    except (FileNotFoundError, ValueError):
        return -1


def measure(dx, dy, step, rounds):
    """沿给定方向注入 step*rounds 个 count，返回像素位移 / count。"""
    x0, y0 = cursor_pos()
    for _ in range(rounds):
        send_rel(dx * step, dy * step)
    x1, y1 = cursor_pos()
    moved = abs(x1 - x0) if dx else abs(y1 - y0)
    return moved / (step * rounds)


def main():
    print("=" * 62)
    print("DeskHop 指针倍率 k 标定")
    print("=" * 62)

    sens = read_sensitivity()
    print(f"指针速度滑块 MouseSensitivity = {sens} (1-20, 默认 10 = 第 6 格)")

    if epp_enabled():
        print()
        print("[X] \"提高指针精确度\"当前为开启状态。")
        print("    EPP 下 k 随移动速度变化，无法用标量标定。")
        print("    请到 设置 > 蓝牙和其他设备 > 鼠标 > 其他鼠标设置 >")
        print("    指针选项 取消勾选后重跑本脚本。")
        sys.exit(1)
    print("[OK] EPP 已关闭，k 为常数，可以标定。")

    print()
    print("标定期间请不要移动物理鼠标...")
    park_center()

    # 三种幅度（单事件 count 数 × 事件数），总位移按 k<=3 预留边距
    cases = [
        (25, 8),    # 200 counts
        (75, 4),    # 300 counts
        (250, 1),   # 250 counts
    ]
    kx_list, ky_list = [], []
    for step, rounds in cases:
        kx_list.append(measure(+1, 0, step, rounds))
        kx_list.append(measure(-1, 0, step, rounds))
        ky_list.append(measure(0, +1, step, rounds))
        ky_list.append(measure(0, -1, step, rounds))

    kx = sum(kx_list) / len(kx_list)
    ky = sum(ky_list) / len(ky_list)

    spread_x = (max(kx_list) - min(kx_list)) / kx * 100 if kx else 999
    spread_y = (max(ky_list) - min(ky_list)) / ky * 100 if ky else 999

    print()
    print("-" * 62)
    print(f"k_x = {kx:.4f}   (6 次测量离散度 {spread_x:.2f}%)")
    print(f"k_y = {ky:.4f}   (6 次测量离散度 {spread_y:.2f}%)")

    if spread_x > 2.0 or spread_y > 2.0:
        print()
        print("[X] 幅度间偏差超过 2%：检测到非线性（疑似 EPP 实际生效）。")
        print("    该结果不可用，请确认 EPP 设置后重跑。")
        sys.exit(1)

    if abs(kx - ky) / max(kx, ky) > 0.02:
        print()
        print("[!] 双轴 k 相差超过 2%（罕见，多见于厂商驱动改写管道），")
        print("    建议两轴分别记录。")

    kx_pm = round(kx * 1000)
    ky_pm = round(ky * 1000)
    print()
    print("-" * 62)
    print("标定结果（千分比，1000 = 1.00）：")
    print(f"  K_X = {kx_pm}   K_Y = {ky_pm}")
    print()
    print("两轴一致时固件只需一个标量，取：")
    k = round((kx + ky) / 2 * 1000)
    print(f"  K = {k}")
    print()
    print("把该值写入这台机器的固件配置：webconfig API 字段 87（pointer_k_pm，")
    print("千分比）。本机默认 1000；只有非 1:1 的机器需要设置。")


if __name__ == "__main__":
    main()
