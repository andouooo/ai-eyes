# AI 的眼睛

> 让 AI 看见你的屏幕。一条热键，把当前窗口递给任意 AI 客户端；回答自动读回，不打断你正在做的事。

**应用源码：[`ai-eyes/`](https://github.com/andouooo/ai-eyes/tree/main/ai-eyes)　·　视觉证据层：[`v1/`](https://github.com/andouooo/ai-eyes/tree/main/v1)**

## 它是什么

AI 客户端很聪明，但看不见你的屏幕。想让它看一个报错、一段 UI 或一个游戏画面，通常要：

**截图 → 保存 → 切窗口 → 上传 → 描述 → 复制答案**

AI 的眼睛把这条链压成一条热键：**截 → 问 → 读**。不配 API Key、不装插件、不换客户端。

## 功能

- **控制台**：左对话 / 右实时画面；锁窗 / 选区 / 多屏；10fps 本地预览
- **悬浮球 / 托盘**：关掉控制台就缩成球，点一下继续问
- **游戏内快捷反馈**：`Ctrl+Alt+B`，截图 + 输入 + Enter；Esc 取消
- **自动回读**：DSH 会话文件直读 0.02s + 屏幕 UIA 兜底
- **代码守卫**：发送前语法体检；会话历史、重启补读
- **Windows 单文件 exe**：108MB，双击即用

## 视觉证据层 v1（独立目录 `v1/`）

和上面的控制台应用是**两件事**，代码互不依赖：`v1/` 只做“可审计的窗口截图”，给 agent 用。

```
锁一个窗口 → capture_id → 回答引用 [eye_...] → verify_evidence.py 判 PASS
```

- 按名字 / 进程 / 句柄找窗口，`PrintWindow` 截图（被遮挡也能截）
- 每张证据带 `capture_id`（ULID）/ 时间戳 / `sha256`，落在本机 `~/.ai-eyes`
- `verify_evidence.py` 零模型审计：没引用 → `UNGROUNDED`，旧图 → `STALE`，对不上 → `CONTRADICTED`

详见 [`v1/README.md`](https://github.com/andouooo/ai-eyes/blob/main/v1/README.md)。

## 快速开始

1. 先打开一个支持粘贴图片的 AI 客户端（DeepSeek Harness / ZCode），并登录。
2. 双击 `RivalEye.exe`（程序代号），控制台顶部选择客户端。
3. 输入问题、勾选“附带截图”、回车；或按 `Ctrl+Alt+B`。

## 适配与边界

- 完整验证在 DeepSeek Harness（开源客户端）；ZCode 早期适配；闭源模型 / 客户端（ChatGPT、Claude、Gemini 等）尚未测试
- Windows only；独占全屏需无边框窗口化；中文输入需要键盘焦点

## 技术链路

```
Win32 PrintWindow 采集
  → 剪贴板 + 键击投递
  → 附件缩略图确认
  → DSH JSONL 直读 / UIA 屏幕兜底
  → Qt 控制台 / 悬浮条呈现
```

## 目录

- `ai-eyes/`：完整源码、demo、截图、安装说明
- `v1/`：视觉证据层（窗口捕获 + 证据链 + 审计脚本 + 技能 + 测试）
- 程序文件名仍为 `RivalEye.exe`（开发代号）

## License

MIT
