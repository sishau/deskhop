# DeskHop 开发文档索引

自用分支：上游 deskhop v0.80（GPLv3）的深度定制，双 RP2040 Pico 组成双机 KVM。远程编译：john@10.0.1.125（`-DPICO_SDK_PATH=~/workspace/deskhop/pico-sdk`），git remote = github.com/sishau/deskhop。

## 文档

| 文档                                   | 内容                                                                |
| ------------------------------------ | ----------------------------------------------------------------- |
| [screen-layout.md](screen-layout.md) | 屏幕布局子系统：v4 统一符号平面 + 虚拟通道、指针倍率 k、layout_profile 与 KVM 状态自动跟随、版本与刷板 |
| [hotkeys.md](hotkeys.md)             | 全部固件热键（源码核实）                                                      |

## 快速事实

- 三屏全 1920×1080：左屏 = 公共屏（KVM 盒按钮切换归属）、右屏 = 主机主屏、内屏 = 笔记本主屏。
- 现行固件 **v0.80.9 = 809**；版本编码 u16 = major×1000 + minor×10 + patch。
- 降级/跨大版本刷板必须两板断电逐板 BOOTSEL（版本倒挂会被对板心跳反向覆盖）。
- 改网页或仓库根文件后必须整仓推送 + `--clean-first` 重编，否则嵌入陈旧 config 页。

## 已搁置方向（存档于 git 历史）

- **副显视频源软切（去 KVM 化）**：主机侧 DDC/CI + Windows 拓扑双向切换已端到端验证，但笔记本侧缓解项均被否决，2026-10-13 终审维持 KVM 盒按钮现状；转向 KVM 状态自动跟随（screen-layout.md §5.2）。实测细节见 git 历史中 docs/side-source-switch-proposal.md。

