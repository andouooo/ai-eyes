# AI 的眼睛 · 让 AI 看见你的屏幕

> 一条热键，把当前窗口递给任意 AI 客户端；回答自动读回，不打断你正在做的事。
>
> 程序代号：RivalEye；exe 文件名仍为 `RivalEye.exe`。
>
> 2026 武汉 1024 · AI 黑客松 · OPC / 个人创新

## 它是什么

AI 客户端很聪明，但看不见你的屏幕。想让它看一个报错、一段 UI 或一个游戏画面，通常要：

**截图 → 保存 → 切窗口 → 上传 → 描述 → 复制答案**

AI 的眼睛把这条链压成一条热键：

```
截（锁窗 / 选区 / 多屏）
  → 问（截图 + 问题，投进你正在用的 AI 客户端）
  → 读（会话文件直读 + 屏幕兜底，回答自动回到控制台）
```

不配 API Key、不装插件、不换客户端。工具本身不调用模型——它把截图投给你已经登录的 AI 客户端。

## 功能

- **控制台**：左边对话、右边实时画面；选客户端、锁窗/选区/多屏、10fps 本地预览
- **悬浮球 / 托盘**：关掉控制台就缩成球，点一下继续问
- **游戏内快捷反馈**：`Ctrl+Alt+B`，截图 + 输入 + Enter；Esc 取消；游戏模式排队发送
- **自动回读**：DSH 会话文件直读（0.02s）+ 屏幕 UIA 兜底；Markdown 还原、字号可调
- **代码守卫**：发送前做行级语法体检，白话给改法（Python / C）
- **会话**：历史、自动标题、重启补读、单实例

## 快速开始

### 直接用 exe

1. 先打开一个支持粘贴图片的 AI 客户端（DeepSeek Harness / ZCode），并登录。
2. 双击 `RivalEye.exe`（约 108MB，Windows 10/11，无需 Python）。
3. 顶部选择客户端 → 启动/刷新；输入问题、勾选“附带截图”、回车。
4. 或在任意窗口按 `Ctrl+Alt+B`。

### 从源码

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

常用命令：

```bash
python -m rival_eye.cli assistant   # 控制台 / 悬浮球
python -m rival_eye.cli demo        # 离线雷达 demo（Mock，无需 Key）
python -m rival_eye.cli shot <url> <out.png>
python -m rival_eye.cli watch <url>
```

## 适配情况

| 环节 | 情况 |
|---|---|
| AI 客户端 | DeepSeek Harness 完整适配；ZCode 早期适配；其他客户端走屏幕兜底或需按 UI 适配 |
| 回读 | DSH 会话文件直读（zstd JSONL）+ 屏幕 UIA 兜底 |
| 系统 | Windows 10/11；macOS / Linux 未验证 |
| 全屏 | 独占全屏无法覆盖（Windows 限制），建议游戏改无边框窗口化 |

## 已知限制（如实写）

- 输入中文需要键盘焦点；“不抢焦点 + 打中文”不可兼得。
- 游戏以管理员运行时，本程序需同权限启动（UIPI）。
- 客户端 UI 改版可能导致投递/回读失效，需要适配。
- 图片回复只能拿到 alt 文本；标题字号层级读不回来。

## 可选：竞品雷达（早期能力）

仓库里还保留了最早做的“竞品雷达”CLI：Playwright 定时截图 → VLM 语义对比 → 红框报告 → 飞书/邮件。它现在不是主线，但 demo 可跑：

```bash
python -m rival_eye.cli demo
```

未配置 `VLM_API_KEY` 时走离线 Mock；配置后走真实 VLM（GLM-4V / Qwen-VL / 本地 Ollama，OpenAI 兼容端点）。

## 技术链路

```
Win32 PrintWindow 采集
  → 剪贴板贴图 + 键击投递
  → 输入框附件缩略图轮询（确认送达）
  → DSH zstd JSONL 会话直读（0.02s）/ UIA 屏幕兜底
  → Qt 控制台 / 悬浮条呈现
```

Python + Qt + Win32，PyInstaller 单文件 108MB；核心 MIT 开源。

## 目录

```
rival-eye/
├─ run.py                 # 入口：双击 / 无参数启动桌面端
├─ rival_eye/
│  ├─ cli.py              # assistant / demo / shot / watch
│  ├─ assistant/          # 控制台、悬浮球、投递、回读、代码守卫
│  ├─ capture.py          # Playwright 采集（雷达）
│  ├─ diff.py             # VLM 语义对比（雷达）
│  ├─ report.py           # 红框报告（雷达）
│  └─ notify.py           # 飞书 / 邮件（雷达）
├─ demo/                  # 离线 demo 数据与页面
├─ assets/                # 图标
├─ snapshots/             # 演示截图
└─ build_exe.bat          # 一键打包 Windows exe
```

## License

MIT
