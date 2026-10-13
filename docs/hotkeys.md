# DeskHop 热键速查（v0.80.6）

来源：`src/keyboard.c` 热键表 + `tools/text-channel/` 托盘 + `tools/text_channel_v2.ahk`。
热键在接收键盘的 A 板（键盘板）检测，物理键盘上按键即可，两台机器都有效。

## 匹配规则

- **修饰键**：表中修饰键必须全部按下（额外多按不拦）；**普通键**：必须在同一次按键报告里出现。
- 代码注释里写的 F12（固件升级两组）**并非必需**——匹配逻辑只看"双 Shift + A/B"。
- `acknowledge=true` 的热键触发时板载 LED 会闪一下确认。
- `pass_to_os=false` 的热键按键不透传给操作系统（OS 看不到这次按键）。

## 固件热键（14 组）

### 输出与布局

| 热键 | 动作 | 备注 |
|---|---|---|
| **左Ctrl + CapsLock** | 切换键鼠输出 A↔B | 主切换热键；不透传 OS（CapsLock 状态不变）；被切换锁禁用时无效；toggle 键可经 webconfig 字段 74 改 |
| **右Ctrl + M** | 切换布局 profile P1↔P2 | 副显归属随新主人（P1→主机 / P2→笔记本），对板自动同步；KVM watcher 上线后此键=手动兜底 |
| **右Ctrl + K** | 切换锁 on/off | 锁住/解锁鼠标跨越屏幕切换 |
| **左Alt(右Alt) + 右Ctrl** | 慢速鼠标（zoom）on/off | **会透传给 OS**；作用于当前输出 |

### 锁定与屏保

| 热键 | 动作 | 备注 |
|---|---|---|
| **右Ctrl + L** | 同时锁屏两台机器 | 向两端发 Win+L（macOS 则 Ctrl+Cmd+Q）；不透传 |
| **左Ctrl + 右Shift + G** | 游戏模式 on/off | 禁用屏保/LED 等干扰 |
| **左Ctrl + 右Shift + S** | 启用 pong 屏保 | 作用于**当前活跃输出** |
| **左Ctrl + 右Shift + J** | 启用 jitter 屏保 | 作用于当前活跃输出 |
| **左Ctrl + 右Shift + X** | 禁用屏保 | 作用于当前活跃输出 |

### 配置与维护（谨慎）

| 热键 | 动作 | 备注 |
|---|---|---|
| **左Ctrl + 右Shift + C + O** | 进入配置模式（webconfig） | 退出=网页 Reboot 按钮或 5 分钟超时；进出均软重启，profile 保留（watchdog scratch7）；配置模式 U 盘可拖 UF2 在位升级固件 |
| **左Shift + 右Shift + A** | 本板（A=键盘板）进 BOOTSEL | 直接重启进 ROM U 盘模式刷机 |
| **左Shift + 右Shift + B** | 对板（B=鼠标板）进 BOOTSEL | 向对板发固件升级消息 |
| **右Shift + F12 + D** | **擦除配置**（两板恢复默认） | k/threshold/speed 等全部重置，慎用 |
| **右Shift + F12 + Y** | 记录当前输出的切换边界 y 坐标 | 光标在屏幕上半=记上边界、下半=记下边界；对板同步 |

## 文本通道热键（剪贴板跨机传输）

### 主机（Rust 托盘 deskhop-text-channel.exe，常驻）

| 热键 | 动作 |
|---|---|
| **Ctrl + Alt + U** | 推送当前剪贴板到对板 Pico（8KB 单发缓冲；需自己先复制） |
| **Ctrl + Alt + D** | 从本机 Pico 拉取缓冲到剪贴板（自己再粘贴） |
| **Ctrl + Alt + 0** | 退出托盘 |

### 笔记本（AHK v2：tools/text_channel_v2.ahk）

| 热键 | 动作 |
|---|---|
| **Ctrl + Alt + U** | 推送剪贴板（同上） |
| **Ctrl + Alt + D** | 拉取到剪贴板 |
| **Ctrl + Alt + 0** | 退出脚本 |

### KVM 状态跟随（v0.80.6，无热键）

主机托盘经 `WM_DISPLAYCHANGE` 事件监听显示器数量（2=主机归属 / 1=笔记本归属），
自动向 Pico 发绝对值 profile 命令；1s 去抖 + 5s 对账兜底。与手动热键互不干扰。

## 与系统热键的潜在冲突

- `右Ctrl + L`：不会透传，但 OS 侧 `Win+L` 正常可用。
- `右Alt + 右Ctrl`（慢速鼠标）**会透传**：若某软件绑定了这组键会同时触发。
- `CapsLock` 被固件吃掉（不透传）：需要真 CapsLock 时先按输出切换以外的……直接用 OS 屏幕键盘，或改字段 74 换键。
