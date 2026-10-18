# AI 的眼睛 · v1 视觉证据层

给 agent 用的“窗口截图 + 证据”组件：**锁一个窗口 → 截一张带 `capture_id` 的图 → 回答引用 `[eye_...]` → 审计判 PASS**。

本目录是**完整、可独立运行**的一份，不依赖仓库根目录的 RivalEye 应用（那套控制台/回读/热键与本层无关）。

## 目录

```text
v1/
  rival_eye/
    eyes.py       # 找窗口 / PrintWindow 截图 / CLI（list capture latest）
    evidence.py   # 证据落盘：ULID ID、sha256、index.jsonl、latest.json
  scripts/
    verify_evidence.py   # 零模型审计：UNGROUNDED / MISSING / STALE / CONTRADICTED
  skills/ai-eyes/SKILL.md  # 给 agent 的技能说明（引用格式与四条硬规则）
  tests/
    test_eyes.py         # 12 项冒烟测试，零依赖
    answer_good.md / answer_bad.md  # 审计示例（由 --write-samples 生成）
```

## 快速开始

```powershell
# 方式 A：任意目录，直接给绝对路径（推荐，skill 里也用这种）
python "E:\桌面\第一个skill\v1\rival_eye\eyes.py" doctor --json
python "E:\桌面\第一个skill\v1\rival_eye\eyes.py" list --json
python "E:\桌面\第一个skill\v1\rival_eye\eyes.py" capture --process notepad --json
python "E:\桌面\第一个skill\v1\rival_eye\eyes.py" latest --json

# 方式 B：进目录后用模块调用（两种等价）
cd v1
pip install -r requirements.txt     # 只有 Pillow；winocr 可选（--ocr 才用）
python -m rival_eye.eyes list --json
```

`doctor` 会检查 Python / Pillow / 证据目录可写 / 能枚举到窗口 / DPI 感知，现场演示前先跑一次。

证据落在 `~/.ai-eyes`（`AI_EYES_HOME` 可改）：`captures/*.png` + `index.jsonl` + `latest.json`，只留最近 200 张 PNG。

## 验收

```powershell
python tests\test_eyes.py                        # 11 项全过
python scripts\verify_evidence.py --write-samples
python scripts\verify_evidence.py --answer tests\answer_good.md   # PASS, exit 0
python scripts\verify_evidence.py --answer tests\answer_bad.md    # UNGROUNDED, exit 1
```

退出码：`0` 成功 / `2` 找不到窗口（含歧义）/ `3` 捕获失败（最小化、黑屏）/ `4` 参数错误。

## 边界

- 只读：不改目标窗口、不抢焦点、不注入按键；
- 只本地：不联网、不上传；
- 网页内容优先用 DOM/控制台（browser-skill），这里解决的是“没有 DOM 的窗口”；
- 敏感窗口（1Password / KeePass / Bitwarden / 网银）不截。

详见 `skills/ai-eyes/SKILL.md`。
