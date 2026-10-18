---
name: ai-eyes
description: 按名字/进程/句柄捕获 Windows 窗口，返回带 capture_id 的截图证据；用于“看一眼这个窗口/页面/报错”，并把视觉结论绑定到证据。
---

# AI 的眼睛（ai-eyes）

让“看见”可验证：**锁一个窗口 → 截一张带 `capture_id` 的图 → 回答引用 `[eye_...]` → 审计判 PASS**。

工具本身不调用模型、不联网、不上传。它只做一件事：把“某个窗口此刻的样子”变成一条可追溯的证据。

## 何时用

- **原生窗口**：桌面客户端、IDE、播放器、Office、记事本
- **游戏 / 独占全屏**：对方已设为无边框窗口化时可以截（独占全屏截不到）
- **终端**：PowerShell / cmd / Windows Terminal 的输出（窗口被别的窗口挡住也能截）
- **PDF、画布、自绘 UI**：没有 DOM 可读的地方
- **浏览器里“看不清”的界面**：想确认按钮颜色、报错样式、布局错位时，像素比 DOM 更接近用户所见

## 何时不用

- **网页内容和交互** → 先用 `browser-skill`：DOM、控制台、网络请求比像素更准，还能点击填表
- **需要滚动/点击才能看到的内容** → browser-skill
- **敏感窗口** → 直接拒绝，不截：
  `1Password` / `KeePass` / `Bitwarden` / 密码管理器 / 网银 / 支付页面 / 含密码的输入框

## 安装（一次）

技能目录自带代码，只要把整个 `ai-eyes/` 放进 DSH 的用户技能目录即可：

```powershell
# 在仓库的 v1 目录里执行（或把整个 ai-eyes/ 手动拷进技能目录）
powershell -ExecutionPolicy Bypass -File .\install.ps1
# → %USERPROFILE%\.dsh\skills\ai-eyes\（SKILL.md + bin\，自包含）
```

依赖只有一个：`pip install Pillow`（`doctor` 会告诉你缺什么）。

## 命令

**任何工作目录都能跑**：命令里用技能自带的路径，不要依赖 `cd`：

```powershell
$eyes = "$env:USERPROFILE\.dsh\skills\ai-eyes\bin\rival_eye\eyes.py"

python $eyes doctor --json                             # 自检：依赖 / 证据目录 / 窗口 / OCR
python $eyes list --json
python $eyes list --json --process chrome
python $eyes capture --process chrome --json
python $eyes capture --query browser --json
python $eyes capture --hwnd 0x0007094C --json
python $eyes capture --process qq --restore --json     # 最小化也能截（会闪一下，不抢焦点）
python $eyes capture --title "Visual Studio Code" --max-width 1280 --json
python $eyes latest --json
```

在源码仓库里开发时，把 `$eyes` 换成 `<仓库>\v1\rival_eye\eyes.py` 即可（同一份代码）。

- JSON 走 stdout，日志走 stderr；中文被转义成 `\uXXXX`，JSON 解析后就是中文。
- 退出码：`0` 成功 / `2` 找不到窗口（含歧义）/ `3` 捕获失败（最小化、黑屏）/ `4` 参数错误。
- 首次使用或换机器先跑 `doctor`：`python` / `pillow` / `evidence_dir` / `windows` 四项必须 `ok`；
  `ocr` / `tkinter` 是可选，缺了不影响截图。

### 窗口对象

```json
{
  "hwnd": 461132,
  "hwnd_hex": "0x0007094C",
  "title": "main.py - Visual Studio Code",
  "pid": 8668,
  "process": "Code.exe",
  "class_name": "Chrome_WidgetWin_1",
  "bounds": [0, 0, 2560, 1380],
  "is_foreground": true,
  "is_minimized": false
}
```

`bounds` = `[left, top, right, bottom]`（屏幕物理像素，已做 DPI 感知）。

### 证据记录（`capture` / `latest` 的输出）

```json
{
  "capture_id": "eye_01M57MC4WG23MZ1S2Z4PH32KM4",
  "ts": "2026-10-18T21:49:15+08:00",
  "seq": 2,
  "window_seq": 1,
  "trigger": "agent",
  "source": "window",
  "locked": true,
  "window": { "hwnd": "0x00020F8E", "title": "_tmp_note.txt - Notepad",
              "process": "Notepad.exe", "pid": 6676, "class_name": "Notepad" },
  "image": { "path": "C:\\Users\\me\\.ai-eyes\\captures\\eye_....png",
             "width": 1280, "height": 758, "bytes": 22705, "sha256": "ebe9…" }
}
```

- `capture_id` 是 ULID，**按字符串排序 = 按时间排序**。
- `restored`（可选，`true`）：这张是临时还原最小化窗口后截的。
- `seq` 全局递增，`window_seq` 同一个 hwnd 递增。
- 证据落在 `~/.ai-eyes`（可用环境变量 `AI_EYES_HOME` 改），只保留最近 200 张 PNG，`index.jsonl` 不删。

## 选择策略

1. **能确定就确定**：优先 `--hwnd`，其次 `--process`。
2. **别名**（大小写不敏感）：
   `browser` / `浏览器` → chrome / msedge / firefox / brave；
   `editor` / `编辑器` → Code / Cursor / notepad；
   `terminal` / `终端` → WindowsTerminal / cmd / powershell / pwsh；
   `game` / `游戏` → **不猜**，返回候选让人选。
3. **默认排除**：DSH 自己、`python.exe`、`pythonw.exe`、各种终端、无标题窗口、工具窗口。
   `--all` 才显示它们；显式 `--process powershell` 仍然可以截。
4. **多候选**：前台窗口优先 → 否则 Z 序最上 → 标题差得大就返回
   `{"ambiguous": true, "candidates": [...]}`，**这时必须问用户选哪个**，不要自己猜。
5. **最小化**：系统会停止为它渲染，像素根本不存在，所以默认返回 `minimized` 而不是黑图。
   加 `--restore` 才会**不激活地还原 → 截一张 → 再放回最小化**（约 0.5 秒，窗口会闪一下，
   但不抢焦点；托盘类应用可能被拉出来）。这样截到的记录会带 `"restored": true`，
   引用时照实说“这是临时还原后截的”。

## 四条硬规则（必须遵守）

1. **同轮证据**：视觉结论必须来自同一轮的 `capture`。没截就说“我没看”，不许根据代码或记忆描述界面。
2. **引用可见**：结论里至少写一处截图里能看到的文字（标题、按钮文案、报错原文），并紧跟 `[eye_...]`。
3. **时间新鲜**：用户操作之后要**重新截**；引用的 `ts` 必须晚于最后一次操作，旧图不算数。
4. **矛盾必报**：代码 / 命令行说“已修复”而截图还在报错时，两个都说
   —— 截图描述现状，代码解释原因，不要二选一。

## 引用格式

```markdown
截图 [eye_01M57MC4WG23MZ1S2Z4PH32KM4] 显示左上角仍是 `ECONNREFUSED 127.0.0.1:8000`，
说明服务没起来，不是前端的问题。
```

一条结论一个 ID；多个窗口各截各的，不要合并引用。

## 审计（回答有没有真看图）

```powershell
$verify = "$env:USERPROFILE\.dsh\skills\ai-eyes\bin\verify_evidence.py"

python $verify --answer my_answer.md              # PASS, exit 0
python $verify --answer my_answer.md --ocr        # 可选：OCR 核对引用文字
python $verify --write-samples                    # 用最新一条证据生成示例答案到 tests\
```

判词：`PASS` / `UNGROUNDED`（有视觉结论没引用）/ `MISSING`（ID 不存在）/ `STALE`（早于最后操作）/ `CONTRADICTED`（进程或文字对不上）。

`--write-samples` 会用最新一条证据重写 `tests/answer_good.md` 与 `tests/answer_bad.md`；换机器或重新截图后先跑一次。

## Token 纪律

- 每个任务**最多 6 张**截图，够用就停。
- 优先 `--max-width 1280`；只看局部时先裁剪再问，不要反复整屏。
- 看完就写摘要（窗口 + 结论 + `[eye_...]`），**不要把图当上下文一直挂着**。
- 同一窗口重复看时，先 `latest --json` 确认时间戳，别截重复的图。
- 一张 1280 的图约 1–2k token；当视频流用是最贵的用法，本工具不做。
