"""verify_evidence 的规则测试：零依赖，直接 ``python tests/test_verify.py`` 跑。

覆盖 5 种判词：PASS / UNGROUNDED / MISSING / STALE / CONTRADICTED。
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import verify_evidence as v  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def _record(cid: str, ts: str, process: str = "chrome.exe", title: str = "GitHub"):
    return {
        "capture_id": cid,
        "ts": ts,
        "trigger": "test",
        "source": "window",
        "locked": True,
        "window": {"hwnd": "0x00000001", "title": title, "process": process,
                   "pid": 1, "class_name": "C"},
        "image": {"path": "", "width": 100, "height": 100, "bytes": 1, "sha256": "x"},
    }


OLD_ID = "eye_01AAAAAAAAAAAAAAAAAAAAAA"
NEW_ID = "eye_01BBBBBBBBBBBBBBBBBBBBBB"
OLD = _record(OLD_ID, "2026-10-18T21:00:00+08:00")
NEW = _record(NEW_ID, "2026-10-18T22:00:00+08:00")


def _run(answer: str, records=(OLD, NEW)):
    with tempfile.TemporaryDirectory(prefix="eyes_verify_") as tmp:
        d = Path(tmp)
        idx = d / "index.jsonl"
        idx.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records),
                       encoding="utf-8")
        ans = d / "answer.md"
        ans.write_text(answer, encoding="utf-8")
        return v.verify(ans, idx)


@test
def test_pass():
    code, lines = _run("截图 [%s] 显示右上角是登录按钮。" % NEW_ID)
    assert code == 0, lines
    assert any(x.startswith("PASS") for x in lines)


@test
def test_ungrounded():
    code, lines = _run("页面显示登录按钮是灰色的。")
    assert code == 1
    assert any("UNGROUNDED" in x for x in lines)


@test
def test_missing():
    code, lines = _run("截图 [eye_01ZZZZZZZZZZZZZZZZZZZZZZ] 显示报错。")
    assert code == 1
    assert any("MISSING" in x for x in lines)


@test
def test_stale():
    code, lines = _run("截图 [%s] 显示报错。" % OLD_ID)
    assert code == 1
    assert any("STALE" in x for x in lines)


@test
def test_process_mismatch():
    rec = _record(NEW_ID, "2026-10-18T22:00:00+08:00", process="notepad.exe", title="x.txt - Notepad")
    code, lines = _run("Chrome 页面里 [%s] 显示登录失败。" % NEW_ID, records=(rec,))
    assert code == 1
    assert any("CONTRADICTED" in x for x in lines)


@test
def test_no_visual_claim():
    code, lines = _run("好的，我这就去改代码。")
    assert code == 0, lines


def main() -> int:
    failed = []
    for fn in TESTS:
        try:
            fn()
        except Exception:
            failed.append(fn.__name__)
            print("FAIL %s" % fn.__name__)
            traceback.print_exc()
        else:
            print("PASS %s" % fn.__name__)
    print("\n%d passed, %d failed, %d total" % (len(TESTS) - len(failed), len(failed), len(TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
