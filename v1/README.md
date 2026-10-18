# AI 的眼睛 · v1 视觉证据层

给 agent 用的“窗口截图 + 证据”组件：**锁一个窗口 → 截一张带 `capture_id` 的图 → 回答引用 `[eye_...]` → 审计判 PASS**。

本目录是**完整、可独立运行**的一份，不依赖仓库根目录的 RivalEye 应用（那套控制台/回读/热键与本层无关）。

## 目录

```text
v1/
  install.ps1                # 装进 %USERPROFILE%\.dsh\skills\ai-eyes（SKILL.md + bin\）
  rival_eye/
    eyes.py       # 找窗口 / PrintWindow 截图 / CLI（list capture latest doctor）
    evidence.py   # 证据落盘：ULID ID、sha256、index.jsonl、latest.json
  scripts/
    verify_evidence.py   # 零模型审计：UNGROUNDED / MISSING / STALE / CONTRADICTED
  skills/ai-eyes/SKILL.md  # 给 agent 的技能说明（引用格式与四条硬规则）
  docs/skill-是什么.md     # 写给不熟悉 skill 机制的人
  tests/
    test_eyes.py         # 15 项冒烟测试，零依赖
    test_verify.py       # 6 项审计规则测试（5 种判词）
    answer_good.md / answer_bad.md  # 审计示例（由 --write-samples 生成）
```

## 安装到 DSH（一次）

```powershell
cd v1
pip install -r requirements.txt          # 只有 Pillow；winocr 可选（--ocr 才用）
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

装到 `%USERPROFILE%\.dsh\skills\ai-eyes\`（`SKILL.md` + `bin\`，**自包含**，不依赖仓库路径）。
卸载：删掉那个目录即可。

## 用法

```powershell
# 安装后，任意目录都能用
$eyes = "$env:USERPROFILE\.dsh\skills\ai-eyes\bin\rival_eye\eyes.py"
python $eyes doctor --json
python $eyes list --json
python $eyes capture --process notepad --json
python $eyes capture --process notepad --crop 0,0,640,400 --json   # 只截一块（原图像素）
python $eyes latest --json

# 或者直接在源码目录里跑（同一份代码）
cd v1
python rival_eye\eyes.py list --json
```

`doctor` 会检查 Python / Pillow / 证据目录可写 / 能枚举到窗口 / DPI 感知，换机器后先跑一次。

**最小化的窗口**：系统不再为它渲染，默认报 `minimized`（不给黑图）。加 `--restore` 会先“不激活地还原”
再截、随后放回最小化（约 0.5 秒，窗口闪一下，但抢不到焦点），记录里会带 `"restored": true`。

**空白风险**：Chromium 被完全遮挡时可能只画出框架、内容区一片白。这种图 mean 很高、不会被判黑，
所以记录里会带 `"blank_warning": true`，提示你重截或加 `--restore`。

`--max-width` 是**等比缩放**（先按原尺寸抓再缩），不会裁掉右边；只想看局部用 `--crop x,y,w,h`。

证据落在 `~/.ai-eyes`（`AI_EYES_HOME` 可改）：`captures/*.png` + `index.jsonl` + `latest.json`，只留最近 200 张 PNG。

## 验收

```powershell
python tests\test_eyes.py                        # 15 项全过
python tests\test_verify.py                      # 6 项全过（审计规则）
python scripts\verify_evidence.py --write-samples
python scripts\verify_evidence.py --answer tests\answer_good.md   # PASS, exit 0
python scripts\verify_evidence.py --answer tests\answer_bad.md    # UNGROUNDED, exit 1
```

退出码：`0` 成功 / `2` 找不到窗口（含歧义）/ `3` 捕获失败（最小化、黑屏）/ `4` 参数错误 / `5` 敏感窗口拒绝。

## 边界

- 只读：不改目标窗口、不抢焦点、不注入按键；
- 只本地：不联网、不上传；
- 网页内容优先用 DOM/控制台（browser-skill），这里解决的是“没有 DOM 的窗口”；
- 敏感窗口（1Password / KeePass / Bitwarden / 网银）不截。

详见 `skills/ai-eyes/SKILL.md`。
