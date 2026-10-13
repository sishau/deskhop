# 副显视频源软切方案（去 KVM 化）— 提案 v1

日期：2026-10-13。前置文档：screen-layout-proposal.md（v4 平面模型，本方案不改其任何几何）。

## 1. 背景与目标

- 现状：左屏（公共屏）经 HDMI/DP KVM 盒**按钮**切换归属，固件无法感知（信息论封口：副显归属比特不在任何可观测量中）。
- 目标：**摘除 KVM 盒**，双机直连显示器两个输入；归属切换由常驻工具经 DDC/CI（VCP 0x60）完成，切换触发与 deskhop 的 profile（P1/P2）绑定。
- 不变式：v4 平面模型、脉冲、k 全部不动——副显账本是虚拟的，不依赖任何一端 Windows 的显示器枚举。

## 2. 实测结论（2026-10-12/13，KVM 已摘除）

| # | 测试 | 结果 | 判定 |
|---|---|---|---|
| A1 | DDC/CI 支持性 | 0x60 = Read+Write，可选 1/15/17 | ✓ 支持 |
| A2 | 输入映射 | **1 = VGA = 主机路，17 = HDMI = 笔记本路**，15 空闲 | ✓ 标定 |
| A3 | 切换 stick | 主机写入 17 → 切 HDMI；写 1 → 切 VGA；再写 17 → 切回。无跳回 | ✓ stick |
| A4 | 非活动源应答 | 监视器在 HDMI 时，主机（非活动源）读 17、写 1 均**成功** | ✓ 双向应答，接力时序要求解除 |
| B  | HPD 行为 | **双向幻影屏实锤**：输入 1 时笔记本仍见副显 (0,-1080)；输入 17 时主机仍见副显 (-1920,3) | ✗ HPD 恒保持 → 拓扑兜底为**必做项** |

几何数据：主机副显 (-1920,3)-(0,1083)，笔记本副显 (0,-1080)-(1920,0)——均与 v4 假设一致（y=3 偏移由 8px 脉冲边距吸收）。注意此前两份 probe 曾被 KVM 盒污染（EDID 常驻），摘除后数据已重采。

## 3. 架构

### 3.1 组件

1. **常驻工具**（Rust，扩展现有 `tools/text-channel/` 托盘 exe，双机各一份，零新第三方依赖）：
   - **DDC/CI**：`dxva2.dll` 的 `GetVCPFeature/SetVCPFeature`（Win32 API，非"依赖"）；
   - **拓扑管理**：`SetDisplayConfig`——归属方 `SDC_TOPOLOGY_EXTEND`，非归属方主机 `SDC_TOPOLOGY_SINGLE`（幸存屏=primary=主显）/ 笔记本 `SDC_TOPOLOGY_INTERNAL`；
   - **状态源**：轮询本机 deskhop 的只读字段 **89 = layout_profile**（vendor HID，与 webconfig 同协议，1 Hz）；启动时即查并校正一次；本地热键手动兜底；
   - 工具以 profile 为唯一真源做幂等动作：profile 未变时不重复发 DDC/拓扑（防抖）。
2. **固件**：预期最小改动——验证 profile 热键变更是否已同步对板（v4 几何依赖双板一致，应已有机制）；字段 89 已随 805 存在，无新增。

### 3.2 切换时序

因 A4（双向应答），DDC 发送端放宽为"**旧归属方发送交出命令**"（约定取确定性，而非被迫）：

```
P1→P2（副显交笔记本）:
  profile 变为 P2（热键）→ 两机工具各自轮询到
  主机工具: SetVCPFeature(0x60, 17)   ← 监视器切 HDMI（此刻主机仍是活动源，A3 已实证）
  主机工具: SetDisplayConfig(SINGLE)  ← 摘掉幻影 DISPLAY2
  笔记本工具: SetDisplayConfig(EXTEND) ← HDMI 上真实点亮（HPD 恒在，无等待）

P2→P1（副显交主机）:
  profile 变为 P1
  笔记本工具: SetVCPFeature(0x60, 1)  ← 切回 VGA
  笔记本工具: SetDisplayConfig(INTERNAL)
  主机工具: SetDisplayConfig(EXTEND)  ← 重新挂载 DISPLAY2
```

双向对称，无握手依赖（各自独立轮询同一 profile 真源，幂等收敛）；切换黑屏 1–2s 仅发生在 DDC 命令生效瞬间。

### 3.3 启动与异常

- 工具启动：查字段 89 → 按当前 profile 校正本机拓扑（**不发** DDC——避免与另一端抢；显示器断电重上电若回默认输入，由"另一端下一次 profile 切换"或手动热键纠正）；
- 手动热键：工具本地热键 = 临时翻转 profile（走 deskhot 现有 profile 热键），工具随之动作——单一真源不变；
- 休眠/恢复：恢复后工具重查字段 89 并幂等校正。

## 4. 文件级改动清单

**固件（预期极小）**
- `src/handlers.c` / `src/keyboard.c`：验证 profile 热键的对板同步（若缺则加一条 UART 消息，~15 行）；
- `webconfig/`、`src/protocol.c`：无改动（字段 89 已在 805）。

**常驻工具（`tools/text-channel/`，增量 ~600-800 行）**
- `src/ddc.rs`（新）：Get/SetVCPFeature 封装 + 0x60 常量与映射（1/17）；
- `src/topology.rs`（新）：SetDisplayConfig 封装（EXTEND/SINGLE/INTERNAL）；
- `src/profile_watch.rs`（新）：字段 89 轮询（复用现有 vendor HID 收发代码）+ 幂等动作状态机；
- `src/main.rs`：装配 + 托盘菜单（当前 profile 显示、手动切换）。

**前置**：双板刷 805+（字段 89 才存在）。

## 5. 风险表

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 笔记本侧 DDC 物理路径未验证（A4 均为主机侧操作） | 低 | P2→P1 切换失灵 | 笔记本装 ControlMyMonitor 复测；失败则约定"主机全责发送"（主机作为非活动源已被 A4 实证可发） |
| `SDC_TOPOLOGY_SINGLE` 在主机上幸存屏选错 | 低 | 主显被摘 | 启动后校验 primary；异常回退 `ChangeDisplaySettingsEx` 零尺寸 DEVMODE 路线 |
| 显示器断电重上电回默认输入 | 中 | 归属与输入错位 | 手动热键/下次 profile 切换纠正；或 OSD 关闭自动源 |
| Windows 拓扑记忆与工具抢状态 | 低 | 中 | 工具幂等 + profile 单一真源；手动 Win+P 会被下一次轮询校正 |
| profile 双板同步缺口 | 低 | 双板几何错乱 | M1 阶段固件验证项（v4 既有依赖） |
| 切换黑屏 1–2s | 确定 | 轻微 | 仅 profile 切换时发生，接受 |
| 窗口位置重排 | 确定 | 轻微 | Windows 按 topology 记忆窗口，比物理拔插温和 |

## 6. 测试清单

- **P0**：笔记本侧 DDC 发送复测（填 A4 的另一半）；端到端 P1↔P2（热键触发，双机拓扑 + 输入源联动正确）；
- **P1**：开机启动校正；显示器断电重上电场景；休眠恢复；
- **P2**：与文本通道（剪贴板）并发；快速连续切换的防抖。

## 7. 阶段划分

- **M1**：工具 MVP——手动触发 DDC + 拓扑（无固件改动，验证端到端可行性）；
- **M2**：profile 轮询联动 + 启动校正 + 防抖状态机；
- **M3**：固件 profile 同步验证/补齐 + 托盘 UI 收尾。
