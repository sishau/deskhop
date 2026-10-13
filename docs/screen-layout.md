# 屏幕布局子系统（Screen Layout）

适用版本：v0.80.9（809）及以上。配套文档：[热键表](hotkeys.md)。

## 1. 硬件拓扑与硬约束

```
┌─────────┬─────────┐
│ 左屏    │ 右屏    │   左屏（公共屏）经 HDMI/DP KVM 盒按钮在两机间切换
├─────────┤ (主机)  │   主机 = Output A（右屏 + 左屏）
│ 内屏    │         │   笔记本 = Output B（内屏 + 左屏）
└─────────┴─────────┘   三屏全 1920×1080
```

- **KB5003637**：Windows 把 HID 绝对坐标钳制到 Windows 主显示器 → 副显（左屏）只能走相对模式接口（index > 1）。推论：**绝对 = 命令**（OS 直接放置，指针倍率 k 不生效）；**相对 = 建议**（报告过 OS 倍率 k 变换）。
- KVM 按钮只切视频路径，Pico 可观测世界逐比特不变（信息论封口）→ 副显归属比特不可直接探测，归属状态由 **layout_profile** 显式维护（热键或 KVM 自动跟随，见 §5）。
- 预算：可执行区 188KB 软限制（memory_map.ld），真实余量看 objdump 的 `.pad`（链接器 size 报告恒 100% 是 .pad 填充假象）；当前 flash 余量 ~112KB、RAM ~59%，无需裁剪。

## 2. 平面模型（v4）

`plane_x / plane_y`（int32，device_t）是唯一真源；原点 = 主显左上角，y 向下。`pointer_x/y`（int16，协议字段 1/2）镜像当前 cell 的屏幕局部坐标——**仅绝对报告更新镜像**（相对报告的 x/y 是增量）。

```
   左屏（副显）      |  右屏（主显）     y ∈ [0, 32767]
   内屏              |  虚拟通道         y ∈ [32768, 65535]
                     x = 0
```

| 区域 | 驱动方式 |
|---|---|
| 主显 / 内屏 | 两机 Windows primary，绝对报告 1:1，零漂移零校准 |
| 副显（左屏） | 相对模式（KB5003637 硬约束），精确累加账本（§4） |
| 虚拟通道 | 无输出，光标隐藏，平面坐标继续行走——内屏↔主显对角路径按真实轨迹走 |

## 3. 边类型（按 layout_profile）

| 边 | P1 (side=A) | P2 (side=B) |
|---|---|---|
| 主显左缘 ↔ 副显右缘 | 同机**自然交接**（零门槛，模式翻转即可） | 跨机：左出 = 停泊+精确脉冲；右出 = 直切（绝对落点） |
| 副显下缘 ↔ 内屏上缘 | 下出 = 直切；上入 = 停泊+精确脉冲 | 同机**自然交接** |
| 主显下缘 ↔ 通道 / 内屏右缘 ↔ 通道 | 通道（进出吃 jump_threshold + 拖拽禁切） | 同 |
| 其余外缘 | 物理墙（钳制） | 同 |

- **自然交接**：OS 光标已物理跨界，固件只翻转坐标模式 + 重锚平面，零报告零落点计算。**禁止加 threshold 间隙**（加间隙 = 固件滞后光标）。
- **直切出副显**：Windows 已把光标钳在出门缘，移动报告即停泊；目标是 primary，绝对放置比例精确。
- **停泊+精确脉冲入副显**（无需跨 Pico 通讯，鼠标板单点持有全部状态）：目标机 primary 上绝对停泊（进主显 = 左下角、进内屏 = 左上角，用户固化偏好）→ 单包相对脉冲，尺寸按 exit 坐标与 side_px 精确计算；落点悬停共享边上方 **8px 边距**（免疫排列偏差）。
- **通道**：进入 = 停泊在出门缘 + 隐藏；退出 = 比例精确落点（两端皆 primary）。

## 4. 指针倍率 k

OS 指针倍率（滑块非 1:1 档）使相对报告过 OS 时被缩放 k 倍，固件必须补偿。

- **存储**：`pointer_k_pm`（千分比，1000 = 1:1）在 **output_t** 内每机器一份，API 字段 **23 = Output A / 53 = Output B**，随其它 output 字段由页面 sendBoth 镜像到两板，无板间同步机制。发送脉冲的板从本地 config 按副显主人取 k。
- **账本**：副显上 `plane += counts × k × 32767 ÷ side_px`（int64 带余数）。
- **脉冲**：落点先按 px 计算（含 8px 边距），再 `counts = px × 1000 ÷ k`。
- **标定方法（权威 = 行为括号法）**：KVM 来回切换读账本反推 k。**SendInput 注入脚本测得的值不可信**——本笔记本 OS 之下有一层只缩放物理鼠标（实测脚本 1.5 vs 真值 1.0）。k 经 webconfig 页 "Pointer Scale k" 两行 UI 直接设定。
- **硬前提**：两机 EPP（Enhance Pointer Precision）必须关闭（k 必须常数）；滑块位置无关。
- **已知实现坑**：park（绝对/ITF0）与 pulse（相对/ITF1）走不同中断端点，端点 ready ≠ 主机已消费——相对脉冲可越过绝对停泊先到。修复：绝对报告提交后 `REL_HOLD_TICKS=10`（5ms）结算窗缓发相对报告。

## 5. layout_profile 切换

profile（`enum layout_profile_e`：HOST = 左屏归 A / LAPTOP = 左屏归 B）是 RAM-only 运行时状态，双板各持一份，**无一致性保证**，靠消息同步。两条入口：

### 5.1 手动热键

Ctrl+M 循环切换（见 docs/hotkeys.md），走同一 `layout_profile_select()`。

### 5.2 KVM 状态自动跟随（v0.80.6+，Rust 托盘只驻主机）

**原理**：按 KVM 盒按钮后主机侧副显 HPD 撤下，`GetSystemMetrics(SM_CMONITORS)` 2↔1 翻转即时且零抖动 → **显示器数量 = KVM 状态比特**。映射 count=2 → P1(HOST) / count=1 → P2(LAPTOP)。

**主机侧（tools/text-channel/ 托盘 exe 内置 watcher）**：
- 事件驱动：`WM_DISPLAYCHANGE` 触发 + 1s 去抖（候选稳定 1s 才动作）+ 5s 对账 SetTimer 兜底；change-driven（启动仅采样基线，防启动同步误发）。
- WM_DEVICECHANGE 切换期有噪声，弃用；S3/S4 休眠用户态冻结、非唤醒源，唤醒后对账自动补切。

**命令通道**：vendor HID `TC_USB_LAYOUT_SELECT = 4`（data[0] = 目标 profile），幂等。

**固件同步链**（`layout_profile_select()`）：
1. `send_value(LAYOUT_SELECT_MSG)` **前置**——UART TX 队列满时 `queue_try_add` 静默丢弃，1000Hz 鼠标转发可占满队列，消息必须先入队；
2. 应用 profile + `set_active_output`；
3. 250ms × 4 幂等重发（process_uart_tx_task）兜底；
4. 同 profile 的请求也向对板补发（否则对板丢包后无法靠重发修复）。

**对板接收**（`handle_layout_select_msg`）：白名单校验 profile 字节（UART 只保 checksum）→ 应用 → 板载 LED 闪一下（同步到达的可观测确认）。

### 5.3 状态观测（API 字段）

| 字段 | 含义 |
|---|---|
| 89 | 本板 `layout_profile`（隐藏 input，暂未上 UI） |
| 88 | 对板 `peer_profile` 只读镜像（心跳 data16[3] 捎带；0xFF = 未知；对板 ≥807 才可信） |

**诊断探针（常驻）**：`GET_REPORT` 的 PULL_EMPTY 应答捎带板载状态——profile / type=4 到达计数 / active_output / config_mode|BOARD_ROLE。主机工具发包前后各读一次，计数不涨 = USB 层丢包，计数涨 = handler 内部问题。探针字节位于应答原本全零区，对 push/pull 零影响。

## 6. 已知取舍

- 切 profile 瞬间光标在副显上：归属翻转，建议先移回主显/内屏。
- 通道内滚轮/按键丢弃（光标不可见）。
- 拖拽禁所有门槛类切换（自然交接除外）；`create_mouse_report` 对 SIDE cell 强制相对（防 profile 翻转瞬间绝对越界报告）。
- 刷后若副屏路径仍异常：下一嫌疑 = 鼠标厂商驱动（G Hub/Synapse）在 OS 之下动指针。

## 7. 版本与刷板

- **版本编码**：u16 = major×1000 + minor×10 + patch（无偏移）。现行 **v0.80.9 = 809**。
- **自动升级**：心跳报版本，对板版本 > 自己即拉对方 256KB 覆盖自己，CRC 过重启。**版本倒挂红线**：低版本板与高版本板相连会被反向覆盖 → **降级/跨大版本必须两板断电逐板刷**（BOOTSEL，或 Ctrl+RShift+C+O 配置模式 U 盘拖 .uf2 在位升级）。升级中 LED 每 4KB 闪，勿切 KVM 断电。
- **构建链**：本机无交叉工具链，编译走 john@10.0.1.125（`-DPICO_SDK_PATH=~/workspace/deskhop/pico-sdk`）。**改网页或仓库根文件（CMakeLists.txt / disk.img）后必须整仓推送 + `--clean-first` 重编**——增量推送 + 普通重编会嵌入陈旧 config 页。验证手段：解剖 UF2（扫块 payload 找 magic `0x0000f00d` + u16 version，注意 payload 长度固定读 blk[32:288]）。
- **改 config_t 结构** → CURRENT_CONFIG_VERSION 递增 → 刷后配置回默认需重设。

## TODO

- webconfig 控制台页面展示两板 profile：本板字段 89（隐藏 input 已存在，差 UI）+ 对板字段 88。链路：改 `webconfig/templates/` → `render.py` 重出 config.htm → `misc/update_disk.py` 重打 disk.img → 远程 `--clean-first` 重编。
