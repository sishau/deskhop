# 二期方案：双机文本通道（Text Channel）

日期：2026-09-30 ｜ 状态：**方案定稿待评审**（评审通过后实施，拟版本 v0.80.3）
分支：feat/screen-layout ｜ 前置：一期 v2 已上板（副显门户，2026-09-30 实测通过）

---

## 1. 需求定稿（2026-09-30 讨论拍板）

| 维度 | 决策 |
|---|---|
| 数据形态 | **纯文本**（URL/代码/中文段落），走对端系统剪贴板，原生 Ctrl+V |
| 单次体量 | ≤ 8KB（UTF-8 约 2700 汉字）；截图/文件明确出局（带宽不够，走网络） |
| 方向 | **主机 → 笔记本为主**，协议双向对称 |
| 触发 | 热键手动：**`Ctrl+Alt+S` 推送 / `Ctrl+Alt+F` 拉取，两端键位完全相同**；不做自动同步 |
| 接收端形态 | AutoHotkey 热键（笔记本已有便携版 AHK 与 Python） |
| USB 红线 | **禁止 MSC/U 盘形态**——通道用 vendor HID（HID 类 0x03，键鼠同族）；config 模式既有 U 盘保留不动 |
| 笔记本环境 | 敏感机（有管控）：按键触发单次调用、无常驻读循环、无键盘钩子、**阅后即焚默认开启** |
| 安全升级口 | 协议预留 ARM 字段（武装热键本期不实现） |

**不可绕过的物理限制**：Pico 是 HID 设备，协议上读不到主机剪贴板 → 发送端必须有主机程序喂文本。
**已否决**：HID 模拟打字传中文（无通用 Unicode 键入方案）；剪贴板自动同步（隐私面大）。

**带宽核算**：瓶颈在 HID 全速中断端点 64B/ms ≈ 理论 64KB/s，实测 30~50KB/s。
8KB 文本 ≈ 0.2~0.3 秒 → KB 级体验为"瞬时"。UART 段（3.68Mbps，7B 文本/包 ≈ 268KB/s）不构成瓶颈。

---

## 2. 架构与数据流

```
[主机 A]                    [A 板 Pico]        [UART]          [B 板 Pico]              [笔记本 B]
Ctrl+Alt+B ──读剪贴板──▶ 分块 ──HID OUT──▶ 转发 TEXT_* 包 ──3.68Mbps──▶ 组装进 8KB 缓冲
 (AHK)                                                                          │ LED 闪烁提示
                                                                                ▼
Ctrl+V ◀──写剪贴板──重组校验──HID IN 拉取(阅后即焚)◀── Ctrl+Alt+F (AHK)
```

- 两板 UART 链路常通，**传输与当前 active_output / KVM 状态无关**：可先推后切，切过去直接拉。
- 反向（笔记本 → 主机）走同一套协议，方向对称。

---

## 3. 协议设计

### 3.1 USB 层（vendor HID，normal 模式）

- 新增第三接口 ITF2：IN 0x83 + OUT 0x04，**report 描述符直接复制 config 模式 vendor 接口的现成定义**（该端点已在上游验证可用）。
- 系统枚举结果：键盘 + 鼠标 + "HID-compliant vendor-defined device"。无盘符、无存储节点、零驱动（Windows 内置 hidusb.sys）。
- Report 64B 布局：`[chunk_idx u8][chunk_total u8][payload 62B]`
  - chunk 0 的 payload 前 4 字节 = total_len (u32 LE)，其后为 UTF-8 文本数据。
  - 完整性判据：收满 chunk_total 块且重组长度 == total_len。无独立 CRC（两端链路各有硬件/包级校验；主机重组时长度对齐校验兜底）。

### 3.2 UART 层（新增消息，对齐现有 12B 定长包结构）

| 消息 | 枚举（暂定） | 8B 负载 |
|---|---|---|
| `TEXT_BEGIN_MSG` | 27 | total_len(u32 LE) + type(u8) + 保留(3B) |
| `TEXT_DATA_MSG` | 28 | chunk_idx(u8) + 文本数据(7B) |

- `type`：0x01 = 剪贴板文本（UTF-8）。**通用载荷类型字段**——通知/命令类是未来的 type 0x02+，固件结构不变。
- B 板收到 BEGIN 即重置缓冲（后传覆盖前传，语义简单）。
- 组装完成**无提示动作**（LED 方案已于 2026-09-30 评审否决）。

### 3.3 拉取与状态

- 主机 `HidD_GetInputReport` 拉取：`[seq u8][payload 63B]`，seq = 0xFF 表示 EOF/缓冲空。
- **阅后即焚**：total_len 字节被取尽 → 缓冲失效。未取完再次 FETCH → 从头重取（幂等）。
- 武装口：预留 `TEXT_ARM_MSG`（枚举占用但本期不注册热键、不启用窗口逻辑）。

---

## 4. 固件改动清单（文件级）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src/usb_descriptors.c` | normal 模式描述符加 ITF2（IN/OUT，复制 config 模式 vendor 定义） |
| 2 | `src/include/tusb_config.h` | `CFG_TUD_HID` 2→3、端点缓冲预算核查（**实现首个核查项**） |
| 3 | `src/usb.c` | normal 配置注册第三接口 |
| 4 | `src/include/protocol.h` | 枚举 +2（BEGIN/DATA），预留 ARM |
| 5 | `src/uart.c` | handler 表 +2 行 |
| 6 | `src/text_channel.c` **（新）** | ~150 行：HID OUT→UART 转发；UART→缓冲组装（8KB）；HID IN 拉取（阅后即焚） |
| 7 | `src/include/text_channel.h` **（新）** | 接口声明 |
| 8 | `CMakeLists.txt` | 源文件列表 +1 |

**预算**：flash +3~4KB（余量 116KB，无压力）；RAM +8.5KB（8KB 缓冲 + 状态，余量 ~111KB）。
**版本**：v0.80.3（patch 递增惯例；两板必须同版本，跨板互刷机制已有 RightCtrl+O）。

---

## 5. 主机侧（一个 .ahk 双端复用，两端对称）

`tools/text_channel.ahk`（单文件，两台机器跑同一份，键位完全相同——肌肉记忆零负担）：

| 热键（两端一致） | 动作 |
|---|---|
| `Ctrl+Alt+S`（send） | 读本机 Clipboard → 按 3.1 分块 → `HidD_SetOutputReport` 循环推到本板 → 对板缓冲 |
| `Ctrl+Alt+F`（fetch） | `HidD_GetInputReport` 循环拉取本板缓冲 → 重组校验 → `Clipboard :=` →（可选）`Send ^v` |

- **双向对称**：任一端都能发能收，"主机→笔记本为主"只是使用频率差异，脚本无角色之分。
- 键位定稿依据（2026-09-30 评审）：`Ctrl+Alt+B` 单手难按（B 键跨越）→ 改左手三指自然位的 S/F（小指 Ctrl、拇指 Alt、无名指 S / 食指 F）。键位是脚本顶部常量，嫌三键可改两键（如 `Alt+S`/`Alt+F`，注意劫持应用默认热键的风险）。
- 与笔记本**既有常驻 AHK（防锁屏，每 ~3s 动鼠标）的关系：独立运行，不合并**。理由：AHK 多实例并行互不干扰（各自进程/各自热键/SetTimer）；故障隔离——文本通道脚本崩溃或修改重启，不影响防锁屏；两个脚本变更节奏完全不同。合并仅在用户偏好托盘整洁时执行（把本文脚本的热键与函数块粘进防锁屏脚本尾部即可）。
- 设备发现：SetupDi 枚举 VID/PID + vendor usage page；键位避开了固件热键区（固件只认 RightCtrl+单键）。
- 依赖：便携版 AHK（两机已有）；`hid.dll` 系统内置，零安装零驱动。
- 备选：PowerShell 版（AHK DllCall 受阻时启用，体验等价、代码略长）。

---

## 6. 风险表

| # | 风险 | 等级 | 对策 |
|---|---|---|---|
| R1 | 复合描述符变化 → Windows 重枚举 | 低 | 标准类设备免驱，刷机后首插自动识别 |
| R2 | normal 模式 ITF/端点预算超限 | **中** | 实现第一步即核查 tusb_config 与既有 EP 分配；config 模式已证 ITF2 可行 |
| R3 | AHK DllCall hid 细节（位宽/结构体） | 低 | PowerShell 备选脚本兜底 |
| R4 | 双板版本不一致 → UART 消息不识别 | 低 | 跨板互刷（RightCtrl+O）统一；handler 表对未知消息本就有忽略路径 |
| R5 | 敏感机 DLP 行为告警 | 中 | 按键触发单次调用、无常驻无钩子、阅后即焚；**公司政策合规性由用户自行把握** |
| R6 | 实际吞吐低于核算（任务调度粒度） | 低 | 8KB 上限下最坏 ~0.5s，无感 |

---

## 7. 实施与验证计划

1. **bench**：核查 tusb_config 端点预算 → normal 模式枚举三接口（设备管理器确认无存储节点）
2. **单板回环**：主机推 → 同板拉（先验 USB 层，不动 UART）
3. **双板全链**：主机 `Ctrl+Alt+S` 推 → 切 KVM → 笔记本 `Ctrl+Alt+F` 拉 → 中文 Ctrl+V；反向各一轮
4. **边界**：空缓冲拉取（EOF）、传输中被覆盖、8KB 满载、中断后重推
5. 版本 v0.80.3，实测通过后 patch 计数沿用

## 8. 评审记录

- [x] 缓冲 8KB（RAM 富余，UTF-8 中文 ~2700 字）——通过（2026-09-30）
- [x] 阅后即焚默认开启——通过
- [x] chunk 协议无独立 CRC（长度对齐兜底）——通过
- [x] LED"有新文本"提示——**否决**（2026-09-30）
- [x] 键位定稿：`Ctrl+Alt+S` 推 / `Ctrl+Alt+F` 拉，两端相同（替代 Ctrl+Alt+B，单手工学）
- [x] AHK 独立运行，不与防锁屏脚本合并

## 9. 实现记录（2026-09-30，commit 17fe504，v0.80.3 = 903）

与本文前述设计的差异以本节为准：

1. **USB report 实际为 12B**（§3.1 的 64B 布局假设作废）。vendor report descriptor
   为 `TUD_HID_REPORT_DESC_VENDOR_CTRL`：REPORT_COUNT(12)×REPORT_SIZE(8)。线上
   包 = report_id(1B) + 12B RAW 包；TinyUSB SET_REPORT 剥离 / GET_REPORT 填充
   report_id，固件回调与 AHK 均按 13B 对齐。
2. **无 OUT 中断端点**：推送走 SET_REPORT 控制传输（HidD_SetOutputReport），
   拉取走 GET_REPORT（HidD_GetInputReport），与 AHK 方案一致。
3. **GET 返回包 data[7] 编码**：`0x80|n`（n=1..7）表示末包携带 n 字节文本，
   0 表示满 7B 且还有后续（原 bit0-EOF 设计有末包长度歧义，已修正）。
4. **类型字段**：SET_REPORT 复用 uart_packet_t.type（TC_PUSH_BEGIN=1 /
   TC_PUSH_DATA=2 / TC_PULL_REQ=3），GET 返回 type=5 数据 / 0xFF 空。
5. **文件清单落定**（§4 的 8 文件收敛为）：usb_descriptors.c、usb.c、
   protocol.h、uart.c、main.h、**text_channel.c/h（新建）**、CMakeLists.txt；
   主机侧 tools/text_channel.ahk。debug 构建的 normal 配置不含 vendor 接口
   （CDC 占用 ITF 2/3）。
6. **实测开销**：+492B flash（.pad 118,252B）、RAM 151,312B（57.72%）、
   -Ofast 零警告。webconfig（config 模式）行为不变。
7. **键位变更（2026-09-30）**：S/F 在本机（Output A）已被其他软件注册占用
   （RegisterHotKey 全量探测证实：A C F L O R S T X Z 1 均被占）。改为
   **Ctrl+Alt+U（Upload，推送）/ Ctrl+Alt+D（Download，拉取）**，方向语义
   直白，U/D 在探测中均空闲。注意探测盲区：键盘钩子类热键不可见，以实际
   按键为准；若笔记本端也冲突，改脚本顶部两个常量即可。
   （2026-09-30 二次变更）：Ctrl+Shift 池探测仅 A/W 被占，S/F 均空闲，最终
   改定 **Ctrl+Shift+S（Send，推送）/ Ctrl+Shift+F（Fetch，拉取）**。注意
   浏览器/IDE 的应用内 Ctrl+Shift+S/F（另存为/全局搜索）会被全局热键拦截。
   （2026-09-30 三轮定稿）：用户否决字母键位——浏览器/Office 中另存为/查找
   等应用内快捷键使用频繁，全局热键拦截不可接受。应用内绑定盘点：Ctrl+Shift
   字母几乎全被浏览器/Office 高频占用；**数字组合零绑定**（浏览器切标签是
   不带 Shift 的 Ctrl+数字）。最终定稿 **Ctrl+Shift+1 推送 / Ctrl+Shift+2
   拉取**（全局注册全 FREE 已探测，应用内零冲突）。热键讨论历三轮：
   Ctrl+Alt+S/F（占）→ Ctrl+Alt+U/D（换）→ Ctrl+Shift+S/F（拦）→ 数字。
7. **主机侧工具改为 Rust（2026-10-08）**：`tools/text-channel/`（Cargo 项目，
   **零第三方依赖**，手写 FFI 调 hid.dll/setupapi.dll/user32/shell32）。
   产物 `deskhop-text-channel.exe` ~150KB 单文件、免安装免 AHK、后台托盘常驻，
   两机跑同一 exe——顺带解决"禁 U 盘笔记本怎么传脚本"（exe 走任意网络渠道）。
   热键：**Ctrl+Shift+1 = 清空剪贴板→^c 复制选中→推送**（1.5s 超时轮询新剪贴板）、
   **Ctrl+Shift+2 = 拉取→写剪贴板→^v 贴到光标处**、Ctrl+Shift+0 / 托盘右键退出。
   粘贴落点说明：Windows 粘贴永远落在键盘焦点（=最后点击位置），非鼠标指针。
8. **AHK v1 推送路径缺陷修复（2026-10-08）**：原 SendPacket 从不写
   pkt[0]=report_id / pkt[1..2]=0xAA 0x55，TinyUSB 因 report_id 不匹配不剥
   首字节 → bufsize=13≠12 整包被固件拒绝，推送从未可用（拉取路径正常）。
   已在 SendPacket 内统一补全帧头。AHK 版保留作参考实现，主推 Rust 版。
9. **最小 Python 版（2026-10-08）**：`tools/text_channel_min.py`，纯标准库
   （ctypes 直调 hid.dll/setupapi.dll/user32/kernel32），~230 行，免安装。
   面向笔记本（敏感环境：无常驻进程、无热键、无钩子，单次调用即退）。
   用法：`push [file|-]`（默认推剪贴板）`pull`（拉取→剪贴板+stdout）。
   协议逻辑经 mock HID 回环测试（帧头/校验和/UTF-8 边界截断/d7 编码往返）。
10. **AHK v2 版（2026-10-08）**：`tools/text_channel_v2.ahk`，行为与 Rust exe
   对齐的单键流（Ctrl+Shift+1 复制选中并推 / Ctrl+Shift+2 拉取并贴光标 /
   Ctrl+Shift+0 退出），v2 语法要点：NumPut 支持 (Type, Value, ..., Target,
   Offset) 多值形式，但 **NumGet 只有 (Target [, Offset, Type]) 一种形式**、
   Buffer 替代 VarSetCapacity、StrPut(str, buf, "UTF-8") 返回含 null 字节数、
   SetTimer(闭包)。Send("^c") 在按住 Ctrl+Shift 时由 v2 自动临时松 Shift，
   无需手动 tap_with_ctrl。
11. **v2 NumGet 换序修复 + v1 脚本删除（2026-10-08）**：用户笔记本实测 v2
   脚本在 SendPacket 的 `NumGet("uchar", pkt, ...)` 处报 Invalid parameter(s)
   （NumPut type-first 全部通过后才炸到 NumGet）——AHK v2.0 的 NumGet 无
   type-first 形式，仅 NumPut 有。全脚本 7 处 NumGet 统一改为
   (Target, Offset, Type)。**text_channel.ahk（v1）按用户要求删除**，AHK
   线仅保留 v2 版；其 VarSetCapacity / 裸 setupapi 导出名等历史坑随之归档。
12. **交互重设计（2026-10-09）**：键注入全线撤除。SSH 会话中注入 ^c 会
    SIGINT 远端进程（有害），mstsc 全屏热键/注入不可达。定稿为纯推拉：
    脚本永不模拟按键，复制粘贴全由用户完成（终端 Ctrl+Shift+C / 右键，
    普通应用 ^c / ^v）。热键改 **Ctrl+Alt+U = 推送当前剪贴板 / Ctrl+Alt+D =
    拉取到剪贴板 / Ctrl+Alt+0 = 退出**（Ctrl+Alt 池探测空闲；数字行
    Shift 组合难按被否）。Rust exe 删除 SendInput/GetAsyncKeyState 整块
    （exe 缩至 148KB），AHK v2 与笔记本合并脚本同步。RDP 场景多数由
    mstsc 自带剪贴板重定向覆盖。
