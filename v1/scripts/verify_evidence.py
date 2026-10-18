"""证据审计：答案里的视觉结论必须绑定真实、新鲜、对得上窗口的截图（零模型，纯规则）。

用法：
  python scripts/verify_evidence.py --answer tests/answer_good.md
  python scripts/verify_evidence.py --answer tests/answer_bad.md --index C:\\Users\\me\\.ai-eyes\\index.jsonl
  python scripts/verify_evidence.py --answer tests/answer_good.md --ocr      # 比对引用文字
  python scripts/verify_evidence.py --write-samples                          # 用最新证据重写示例

判定：
  PASS         引用真实存在，窗口/时间都对得上
  UNGROUNDED   有视觉结论，却没有 [eye_...] 引用（没看就编）
  MISSING      引用的 ID 不在 index 里（编 ID / 旧机器的图）
  STALE        引用的图早于“最后操作时间”（拿旧图）
  CONTRADICTED 答案说的进程/文字与截图对不上

退出码：全部 PASS 为 0，有任何问题为 1，参数/文件错误为 2。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

AI_EYES_HOME = Path(os.environ.get("AI_EYES_HOME", str(Path.home() / ".ai-eyes")))

# 视觉结论的词：出现这些词的行，必须有 [eye_...]
VISUAL_WORDS = (
    "显示", "看到", "看见", "页面", "按钮", "报错", "界面", "截图", "画面",
    "弹窗", "输出了", "写着", "上是", "里面是",
    "screenshot", "screen shows", "on screen", "the page", "the window",
    "the button", "the error", "looks like",
)

# 答案里提到某个 App → 期望证据的进程
APP_KEYWORDS = {
    "chrome": "chrome.exe",
    "谷歌浏览器": "chrome.exe",
    "edge": "msedge.exe",
    "firefox": "firefox.exe",
    "vscode": "code.exe",
    "vs code": "code.exe",
    "visual studio code": "code.exe",
    "记事本": "notepad.exe",
    "notepad": "notepad.exe",
    "powershell": "powershell.exe",
    "cmd": "cmd.exe",
    "微信": "weixin.exe",
    "wechat": "weixin.exe",
    "qq": "qq.exe",
}

CITE_RE = re.compile(r"\[(eye_[0-9A-Za-z]{16,32})\]")
TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)")
DATETIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})")
QUOTE_RES = (
    re.compile(r"“([^”\n]{2,60})”"),
    re.compile(r"「([^」\n]{2,60})」"),
    re.compile(r'"([^"\n]{2,60})"'),
    re.compile(r"`([^`\n]{2,60})`"),
)


# ---------------------------------------------------------------- 读取

def load_index(path: Path) -> list[dict]:
    recs: list[dict] = []
    if not path.exists():
        return recs
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("capture_id"):
            recs.append(obj)
    return recs


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 规则

def _visual_lines(text: str) -> list[tuple[int, str]]:
    """返回 (行号, 行文本)，只含带视觉词、不是标题/代码块的正文行。"""
    out = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        low = line.lower()
        if any(w.lower() in low for w in VISUAL_WORDS):
            out.append((i, line))
    return out


def _mentioned_times(text: str) -> datetime | None:
    """答案里提到的“最后操作时间”：取最晚的一个。"""
    found: list[datetime] = []
    for m in DATETIME_RE.finditer(text):
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        try:
            found.append(datetime(y, mo, d, hh, mm).astimezone())
        except ValueError:
            pass
    if not found:
        for m in TIME_RE.finditer(text):
            hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
            if hh > 23 or mm > 59 or ss > 59:
                continue
            today = datetime.now().astimezone().date()
            found.append(datetime.combine(today, time(hh, mm, ss)).astimezone())
    return max(found) if found else None


def _expected_processes(text: str) -> set[str]:
    low = text.lower()
    return {proc for key, proc in APP_KEYWORDS.items() if key in low}


def _quotes_near(text: str, cid: str) -> list[str]:
    """引用 ID 所在行（及下一行）里的引号内容，用于 OCR 比对。"""
    lines = text.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if "[%s]" % cid not in line:
            continue
        blob = line + ("\n" + lines[i + 1] if i + 1 < len(lines) else "")
        for rx in QUOTE_RES:
            out.extend(m.group(1).strip() for m in rx.finditer(blob))
    return [q for q in out if q]


def _normalize(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _ocr_text(png: Path, lang: str) -> str:
    from PIL import Image
    from winocr import recognize_pil_sync

    with Image.open(png) as img:
        result = recognize_pil_sync(img.convert("RGB"), lang)
    # winocr 的 sync 版本返回 picklify 后的 dict（不是对象）
    if isinstance(result, dict):
        return str(result.get("text", "") or "")
    return str(getattr(result, "text", "") or "")


# ---------------------------------------------------------------- 主流程

def verify(answer: Path, index: Path, *, use_ocr: bool = False, lang: str = "zh-Hans",
           max_age_minutes: int = 0) -> tuple[int, list[str]]:
    text = answer.read_text(encoding="utf-8", errors="replace")
    recs = load_index(index)
    by_id = {r["capture_id"]: r for r in recs}
    latest = recs[-1] if recs else None

    problems: list[str] = []
    report: list[str] = []

    # 1. 视觉结论必须有引用
    for lineno, line in _visual_lines(text):
        if not CITE_RE.search(line):
            snippet = line if len(line) <= 42 else line[:42] + "…"
            problems.append("UNGROUNDED  第 %d 行视觉结论没有引用：%s" % (lineno, snippet))

    # 2. 引用 ID 必须存在
    cited: list[str] = []
    for m in CITE_RE.finditer(text):
        cid = m.group(1)
        if cid not in cited:
            cited.append(cid)

    if not cited:
        if not problems:
            report.append("PASS  （答案里没有视觉结论，不需要证据）")
            return 0, report
        return 1, problems

    mentioned_at = _mentioned_times(text)
    expected_procs = _expected_processes(text)

    for cid in cited:
        rec = by_id.get(cid)
        if rec is None:
            problems.append("MISSING  %s 不在 index 里（编的 ID，或换了机器）" % cid)
            continue

        window = rec.get("window") or {}
        proc = str(window.get("process", ""))
        title = str(window.get("title", ""))
        ts = parse_ts(rec.get("ts", ""))

        # 3. 时间新鲜：早于“最后操作时间”就是拿旧图
        stale = False
        if ts is not None and mentioned_at is not None:
            stale = ts < mentioned_at
        elif ts is not None and latest is not None:
            latest_ts = parse_ts(latest.get("ts", ""))
            if cid != latest.get("capture_id") and latest_ts is not None:
                same_round = ts.replace(second=0, microsecond=0) == \
                    latest_ts.replace(second=0, microsecond=0)
                stale = (ts < latest_ts - timedelta(minutes=max_age_minutes)) and not same_round
        if stale:
            problems.append("STALE  %s 早于最后操作（证据 %s）" % (cid, rec.get("ts", "?")))

        # 4. 说到的进程必须对得上
        if expected_procs and proc.lower() not in expected_procs:
            problems.append("CONTRADICTED  答案说的是 %s，证据是 %s"
                            % ("/".join(sorted(expected_procs)), proc))

        # 5. 可选 OCR：引用的文字必须真的在截图里
        if use_ocr:
            png = Path(str((rec.get("image") or {}).get("path", "")))
            if not png.exists():
                problems.append("MISSING  %s 的图片文件已不在磁盘上" % cid)
            else:
                try:
                    ocr = _normalize(_ocr_text(png, lang))
                except Exception as exc:      # OCR 失败不该判答案有问题
                    report.append("WARN  %s OCR 失败：%s" % (cid, exc))
                    ocr = ""
                quotes = _quotes_near(text, cid)
                if not ocr:
                    report.append("WARN  %s OCR 没识别出文字（Windows OCR 语言包缺失？），"
                                  "引用文字未核对" % cid)
                else:
                    for quote in quotes:
                        if _normalize(quote) not in ocr:
                            problems.append("CONTRADICTED  引用文字“%s”不在 %s 的截图里"
                                            % (quote, cid))

        if not stale:
            report.append("PASS  %s  (%s / %s)" % (cid, proc, title[:40]))

    if problems:
        return 1, problems + report
    return 0, report


# ---------------------------------------------------------------- 示例文件

GOOD_TEMPLATE = """# 示例：有证据的回答（verify → PASS）

> 本文件由 `--write-samples` 生成，引用生成时的最新一条证据；重新取图后请再跑一次，否则会判 STALE。

我截了当前窗口 [{cid}]，截图显示的是一个记事本窗口，标题是 {title}。
"""
BAD_TEMPLATE = """# 示例：没看就编的回答（verify → UNGROUNDED）

我看了浏览器，页面显示登录按钮是灰色的。
"""


def write_samples(out_dir: Path, index: Path) -> int:
    recs = load_index(index)
    if not recs:
        print("没有可用证据：先跑一次 capture 再生成示例", file=sys.stderr)
        return 2
    rec = recs[-1]
    out_dir.mkdir(parents=True, exist_ok=True)
    title = str((rec.get("window") or {}).get("title", ""))
    (out_dir / "answer_good.md").write_text(
        GOOD_TEMPLATE.format(cid=rec["capture_id"], title=title), encoding="utf-8")
    (out_dir / "answer_bad.md").write_text(BAD_TEMPLATE, encoding="utf-8")
    print("已写入 %s 和 %s（引用 %s）"
          % (out_dir / "answer_good.md", out_dir / "answer_bad.md", rec["capture_id"]))
    return 0


# ---------------------------------------------------------------- 入口

def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        prog="verify_evidence.py",
        description="检查回答里引用的截图证据是否真实、新鲜、对得上窗口（零模型）。")
    ap.add_argument("--answer", help="要审计的回答文件（.md/.txt）")
    ap.add_argument("--index", default=str(AI_EYES_HOME / "index.jsonl"),
                    help="证据索引，默认 ~/.ai-eyes/index.jsonl")
    ap.add_argument("--ocr", action="store_true", help="额外用 OCR 核对引用文字")
    ap.add_argument("--lang", default="zh-Hans", help="OCR 语言（默认 zh-Hans）")
    ap.add_argument("--max-age-minutes", type=int, default=0,
                    help="答案没写时间时，允许比最新一张旧多少分钟（默认 0 = 只认最新或同一轮）")
    ap.add_argument("--write-samples", nargs="?", const="tests", metavar="DIR",
                    help="用最新证据重写 answer_good.md / answer_bad.md 后退出")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    index = Path(args.index)
    if args.write_samples:
        return write_samples(Path(args.write_samples), index)

    if not args.answer:
        ap.print_help()
        return 2
    answer = Path(args.answer)
    if not answer.exists():
        print("找不到回答文件：%s" % answer, file=sys.stderr)
        return 2
    if not index.exists():
        print("找不到证据索引：%s（先跑一次 capture）" % index, file=sys.stderr)
        return 2

    code, lines = verify(answer, index, use_ocr=args.ocr, lang=args.lang,
                         max_age_minutes=args.max_age_minutes)
    for line in lines:
        print(line)
    passed = sum(1 for x in lines if x.startswith("PASS"))
    print("\n%s：%d 条通过，%d 个问题"
          % ("PASS" if code == 0 else "FAIL", passed,
             sum(1 for x in lines if x.startswith(("UNGROUNDED", "MISSING", "STALE", "CONTRADICTED")))))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
