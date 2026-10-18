"""AI 的眼睛 · 证据层：截图落盘 + 可排序 ID + 索引 + latest。

一条证据 = 一张 PNG + 一行 ``index.jsonl`` + 一份 ``latest.json``。
ID 是 ULID（时间前缀），所以按字符串排序就是按时间排序，审计脚本不用解析时间。

约定（交接文档 §3.3）：
- 只写本机 ``~/.ai-eyes``（可用 ``AI_EYES_HOME`` 覆盖）；
- 不联网、不上传；
- 先写 ``*.tmp`` 再 ``os.replace``，避免读到半个文件；
- 默认只留最近 200 张 PNG（index 不删，历史仍可追溯）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

AI_EYES_HOME = Path(os.environ.get("AI_EYES_HOME", str(Path.home() / ".ai-eyes")))
CAPTURE_DIR = AI_EYES_HOME / "captures"
INDEX = AI_EYES_HOME / "index.jsonl"
LATEST = AI_EYES_HOME / "latest.json"

KEEP = 200          # 保留最近多少张 PNG
_LOCK = threading.Lock()

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# ---------------------------------------------------------------- ID

def _ulid(ts: float | None = None) -> str:
    """26 字符 ULID：48 位毫秒时间 + 80 位随机，字典序 = 时间序。"""
    ms = int((time.time() if ts is None else ts) * 1000)
    chars = []
    for _ in range(10):                  # 48 bit / 5 bit per char
        chars.append(_CROCKFORD[ms & 0x1F])
        ms >>= 5
    ts_part = "".join(reversed(chars))
    rand = int.from_bytes(secrets.token_bytes(10), "big")   # 80 bit = 16 个 5 bit 字符
    chars = []
    for _ in range(16):
        chars.append(_CROCKFORD[rand & 0x1F])
        rand >>= 5
    rand_part = "".join(reversed(chars))
    return ts_part + rand_part


def new_capture_id() -> str:
    return "eye_" + _ulid()


# ---------------------------------------------------------------- 读写

def _ensure_dirs() -> None:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj) -> None:
    _atomic_write_bytes(
        path,
        json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def read_index() -> list[dict]:
    """读 index.jsonl；坏行直接跳过（手工编辑 / 断电都不会让整个索引报废）。"""
    if not INDEX.exists():
        return []
    out: list[dict] = []
    text = INDEX.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("capture_id"):
            out.append(obj)
    return out


def _last_seq() -> int:
    recs = read_index()
    for rec in reversed(recs):
        try:
            return int(rec.get("seq") or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _window_seq(hwnd_hex: str, recs: list[dict]) -> int:
    n = 0
    for rec in recs:
        if str((rec.get("window") or {}).get("hwnd", "")).upper() == hwnd_hex.upper():
            n += 1
    return n + 1


# ---------------------------------------------------------------- 写证据

def capture(img, window, trigger: str = "agent", source: str = "window",
            locked: bool = False) -> dict:
    """把一张 PIL 图写成一等证据，返回记录（与 index 里那行一致）。"""
    if img is None:
        raise ValueError("capture() 收到空图")
    _ensure_dirs()

    with _LOCK:
        cid = new_capture_id()
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()

        path = CAPTURE_DIR / (cid + ".png")
        _atomic_write_bytes(path, data)

        recs = read_index()
        hwnd_hex = "0x%08X" % int(window.get("hwnd", 0))
        now = datetime.now().astimezone()

        record = {
            "capture_id": cid,
            "ts": now.isoformat(timespec="seconds"),
            "seq": _last_seq() + 1,
            "window_seq": _window_seq(hwnd_hex, recs),
            "trigger": trigger,
            "source": source,
            "locked": bool(locked),
            "window": {
                "hwnd": hwnd_hex,
                "title": window.get("title", ""),
                "process": window.get("process", ""),
                "pid": int(window.get("pid", 0) or 0),
                "class_name": window.get("class_name", ""),
            },
            "image": {
                "path": str(path),
                "width": int(img.width),
                "height": int(img.height),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        }

        line = json.dumps(record, ensure_ascii=False)
        with open(INDEX, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

        _atomic_write_json(LATEST, record)
        _prune()

    return record


def _prune(keep: int = KEEP) -> None:
    """只删最旧的 PNG，index 保留，审计仍然查得到历史。"""
    try:
        pngs = sorted(CAPTURE_DIR.glob("eye_*.png"), key=lambda p: p.name)
        for old in pngs[:-keep] if len(pngs) > keep else ():
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def latest() -> dict | None:
    """最近一条证据；latest.json 丢了就退回索引最后一行。"""
    if LATEST.exists():
        try:
            obj = json.loads(LATEST.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and obj.get("capture_id"):
                return obj
        except (json.JSONDecodeError, OSError):
            pass
    recs = read_index()
    return recs[-1] if recs else None


def find(capture_id: str) -> dict | None:
    """按 capture_id 查证据（verify_evidence.py 用）。"""
    want = (capture_id or "").strip()
    for rec in reversed(read_index()):
        if rec.get("capture_id") == want:
            return rec
    return None
