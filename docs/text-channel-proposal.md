# 二期：双机文本通道（已实现，v0.80.3 = 903）

设计定稿 2026-09-30（commit 3a5e444），实现 2026-09-30（commit 17fe504）。本文为**实现后压缩版**：只保留现行事实与教训，设计论证与热键三轮变迁史见 git 历史。

## 1. 需求与硬限制

- 纯文本 ≤8KB（UTF-8 ~2700 汉字），写入对端系统剪贴板原生 Ctrl+V；截图/文件出局（走网络）。
- **物理限制**：Pico 是 HID 设备，协议上读不到主机剪贴板 → 发送端必须有主机程序喂文本，不可绕过。
- **USB 红线**：数据通道禁止 MSC/U 盘形态——用 vendor HID（HID 类 0x03，键鼠同族）；config 模式既有 U 盘保留。
- 笔记本是敏感环境：按键触发单次调用、无常驻读循环、无键盘钩子、阅后即焚默认。
- 带宽：HID 全速中断端点实测 30~50KB/s，8KB ≈ 0.3s"瞬时"；UART 段 3.68Mbps 不构成瓶颈。
- 传输与 active_output / KVM 状态无关：可先推后切，切过去直接拉。

## 2. 协议（实现落定版）

**normal 模式已挂 vendor HID**（ITF 2 / EP 0x83，描述符与 config 模式同号分流）：config 模式走 webconfig 协议（validate_packet 白名单），normal 模式只走文本通道；DH_DEBUG 构建 CDC 占 ITF2/3，vendor 接口 `#ifndef DH_DEBUG` 排除。

- **USB 层**：report 实际为 **12B RAW**（不是设计期的 64B 假设）。线上包 = `[0x06][0xAA][0x55][type][d0..d7][xor(d0..d7)]` 共 13B；TinyUSB SET_REPORT 剥 report_id / GET_REPORT 填 report_id。
- **SET_REPORT（推送）**：`TC_PUSH_BEGIN=1`（d0..d1 = 总长 LE）/ `TC_PUSH_DATA=2`（d0=seq, d1..d7=文本）→ UART `TEXT_BEGIN=27` / `TEXT_DATA=28`（8B 负载）转发。发送板纯中继不落缓冲；接收板 8KB 缓冲组装（seq 回绕校验，断裂 abort）。
- **GET_REPORT（拉取）**：type=5，d0..d6 = 文本，**d7 = `0x80|n` 标记末包携带 n 字节，0 = 满 7B 还有后续**；`0xFF` = 缓冲空。`PULL_REQ=3` 重置读端 offset。单缓冲阅后即焚（读完 memset）。
- type 字段通用化：剪贴板文本是 type 0x01 类语义，未来通知/命令可扩展，固件结构不变。

## 3. 固件改动（已合入）

`usb_descriptors.c`（normal 描述符 +ITF2）、`usb.c`、`tusb_config.h`、`protocol.h`（TEXT_* 枚举）、`uart.c`、**`text_channel.c/h`（新建 ~150 行）**、`CMakeLists.txt`。实测开销：+492B flash、RAM 151,312B（57.72%）、-Ofast 零警告。

## 4. 主机侧（三线并存）

| 工具 | 形态 | 适用 |
|---|---|---|
| **Rust 托盘 exe（主推）** `tools/text-channel/` | Cargo 零第三方依赖、手写 #[link] FFI；产物 deskhop-text-channel.exe ~148KB 单文件免安装，两机同一 exe | 日常双机；顺带解决"禁 U 盘笔记本怎么传脚本"（exe 走网络渠道） |
| AHK v2 `tools/text_channel_v2.ahk` | 便携 AHK，无编译 | 备用/参考实现 |
| 零足迹 Python `tools/text_channel_min.py` | 纯标准库 ctypes 直调 hid/setupapi；`push [file|-]` / `pull`，单次调用即退 | 敏感环境（无常驻、无热键、无钩子） |

- **热键最终定稿：`Ctrl+Alt+U` 推送当前剪贴板 / `Ctrl+Alt+D` 拉取到剪贴板 / `Ctrl+Alt+0` 退出**。脚本永不模拟按键，复制粘贴全由用户完成。
- **键注入全线撤除（2026-10-09）**：SSH 会话中注入 ^c 会 SIGINT 远端进程（有害），mstsc 全屏热键不可达；早期"复制选中并推送/拉取并贴光标"的 SendInput 流整块删除（Rust exe 缩至 148KB）。RDP 场景多数由 mstsc 自带剪贴板重定向覆盖。
- 热键位演进（三轮）：Ctrl+Alt+S/F（被 RegisterHotKey 占）→ Ctrl+Alt+U/D → Ctrl+Shift+S/F（拦应用内）→ Ctrl+Shift+1/2（数字零绑定）→ 最终回归 Ctrl+Alt+U/D（配合"不注入"重设计，数字行 Shift 组合难按被否）。

## 5. 踩坑记录（教训）

1. **推送帧头缺失 = 从未可用**（2026-10-08 发现）：原 AHK SendPacket 不写 pkt[0]=report_id / pkt[1..2]=0xAA 0x55 → TinyUSB 因 report_id 不匹配不剥首字节，bufsize=13≠12 整包被固件拒绝（拉取路径正常，极具迷惑性）。修复 commit cb85313。
2. **AHK v2 NumGet 无 type-first 形式**（仅 NumPut 有）：`NumGet("uchar", pkt, ...)` 运行时报 Invalid parameter(s)，全脚本 7 处须写 `(Target, Offset, Type)`。
3. **GET_REPORT buffer[0] 须预填 report_id**，否则设备 stall。
4. **ctypes 句柄位宽**：HDEVINFO/HANDLE 在 x64 是 pointer-sized，不显式 restype/argtypes 会被截成 32 位 c_int，SetupDiEnumDeviceInterfaces 在 index 0 直接失败。
5. 设备发现：SetupDi 枚举按 `vid_1209&pid_c000` + `mi_02` 过滤。

## 6. 验证状态

单板回环 / 双板全链 / 中文与 8KB 边界已随 903 上机验证通过（2026-09-30）。当前板固件为 801（平面模型版，含文本通道，版本号体系重置）——刷板流程见 docs/screen-layout-proposal.md §5。
