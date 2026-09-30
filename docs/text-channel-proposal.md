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
| 触发 | 热键手动推送（发送端）+ 热键拉取（接收端）；不做自动同步 |
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
- 组装完成 → 可选 LED 提示"有新文本"（复用 led.c，P2 增强）。

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
| 6 | `src/text_channel.c` **（新）** | ~150 行：HID OUT→UART 转发；UART→缓冲组装（8KB）；HID IN 拉取（阅后即焚）；LED 钩子 |
| 7 | `src/include/text_channel.h` **（新）** | 接口声明 |
| 8 | `CMakeLists.txt` | 源文件列表 +1 |

**预算**：flash +3~4KB（余量 116KB，无压力）；RAM +8.5KB（8KB 缓冲 + 状态，余量 ~111KB）。
**版本**：v0.80.3（patch 递增惯例；两板必须同版本，跨板互刷机制已有 RightCtrl+O）。

---

## 5. 主机侧（一个 .ahk 双端复用）

`tools/text_channel.ahk`（单文件，两端同脚本，热键/命令行双模式）：

| 端 | 热键 | 动作 |
|---|---|---|
| 主机（发送） | `Ctrl+Alt+B` | 读 Clipboard → 按 3.1 分块 → `HidD_SetOutputReport` 循环 |
| 笔记本（接收） | `Ctrl+Alt+F` | `HidD_GetInputReport` 循环拉取 → 重组 → `Clipboard :=` →（可选）`Send ^v` |

- 设备发现：SetupDi 枚举 VID/PID + vendor usage page；**避开固件热键键位**（固件只认 RightCtrl+单键）。
- 依赖：便携版 AHK（两机已有）；`hid.dll` 系统内置，零安装零驱动。
- 备选：PowerShell 版（笔记本 AHK DllCall 受阻时启用，体验等价、代码略长）。

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
3. **双板全链**：主机推 → LED 提示 → 切 KVM → 笔记本拉 → 中文 Ctrl+V；反向各一轮
4. **边界**：空缓冲拉取（EOF）、传输中被覆盖、8KB 满载、中断后重推
5. 版本 v0.80.3，实测通过后 patch 计数沿用

## 8. 待评审决策点

- [ ] 缓冲 8KB（RAM 富余，UTF-8 中文 ~2700 字）——可调
- [ ] 阅后即焚默认开启——可关
- [ ] chunk 协议无独立 CRC（长度对齐兜底）——是否升级 CRC32
- [ ] LED"有新文本"提示：P2 增强，首版可不做
