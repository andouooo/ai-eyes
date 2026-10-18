"""eyes / evidence 冒烟测试：零依赖，直接 ``python tests/test_eyes.py`` 跑，pytest 也能收集。

原则（交接文档 §6）：用像素统计判断“有没有截到东西”，不逐张 read_image；
一张 1280 的图只够看一次，剩下的交给 mean / 尺寸 / hash。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageStat          # noqa: E402

from rival_eye import evidence            # noqa: E402
from rival_eye import eyes as eye         # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ---------------------------------------------------------------- 小工具

def _wait(predicate, timeout: float = 12.0, interval: float = 0.15):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _mean(img) -> float:
    return ImageStat.Stat(img.convert("L")).mean[0]


def _cli(*argv: str) -> tuple[int, object]:
    """跑一条 CLI 命令，返回 (exit_code, 解析后的 stdout)。"""
    proc = subprocess.run(
        [sys.executable, "-m", "rival_eye.eyes", *argv],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    out = proc.stdout.strip()
    try:
        return proc.returncode, json.loads(out) if out else None
    except json.JSONDecodeError:
        return proc.returncode, out


FIXTURE_TITLE = "EYES_TEST_WINDOW"
FIXTURE_CODE = (
    "import sys, tkinter as tk;"
    "root = tk.Tk(); root.title(sys.argv[1]); root.geometry('900x600');"
    "tk.Label(root, text=sys.argv[2], font=('Segoe UI', 28)).pack(expand=True);"
    "root.mainloop()"
)


def _find_fixture(title: str):
    for win in eye.list_windows(apply_defaults=False):
        if win["title"] == title:
            return win
    return None


def _capture_ready(hwnd, timeout: float = 5.0):
    """窗口刚出现时头几帧可能是黑的，重试到出图（断言失败再报 None）。"""
    return _wait(lambda: eye.capture_window(hwnd), timeout, interval=0.25)


class _FixtureWindow:
    """自己起一个 Tk 窗口当截图表：不碰用户的记事本 / 浏览器，也不留标签。"""

    def __init__(self, title: str = FIXTURE_TITLE, text: str = "EYES TEST WINDOW"):
        self.title = title
        self.text = text
        self.proc = None
        self.window = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", FIXTURE_CODE, self.title, self.text])
        self.window = _wait(lambda: _find_fixture(self.title), timeout=15)
        if not self.window:
            raise RuntimeError("测试窗口 15 秒内没出现")
        return self.window

    def __exit__(self, *exc):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- 列表与选择

@test
def test_list_windows_shape():
    wins = eye.list_windows()
    assert isinstance(wins, list) and wins, "list_windows 返回空数组"
    required = {"hwnd", "hwnd_hex", "title", "pid", "process", "class_name",
                "bounds", "is_foreground", "is_minimized"}
    for win in wins:
        assert required <= set(win), "缺少字段：%s" % (required - set(win))
        assert isinstance(win["hwnd"], int) and win["hwnd"] > 0
        assert win["hwnd_hex"].startswith("0x")
        assert len(win["bounds"]) == 4
        assert win["pid"] > 0
        assert win["process"].lower().endswith(".exe"), win["process"]
    assert sum(1 for x in wins if x["is_foreground"]) <= 1, "最多一个前台窗口"


@test
def test_default_exclusions():
    wins = eye.list_windows()
    procs = {x["process"].lower() for x in wins}
    for bad in ("python.exe", "pythonw.exe", "powershell.exe", "cmd.exe", "windowsterminal.exe"):
        assert bad not in procs, "%s 没被默认排除" % bad
    assert not any("DeepSeek Harness" in x["title"] for x in wins), "DSH 自身没被排除"
    # --all 应当比默认多（至少能看到终端 / DSH 里的一个）
    assert len(eye.list_windows(apply_defaults=False)) >= len(wins)


@test
def test_resolve_by_hwnd_roundtrip():
    win = eye.list_windows()[0]
    got = eye.resolve_window(hwnd=win["hwnd"])
    assert got is not None and got["hwnd"] == win["hwnd"]
    got = eye.resolve_window(hwnd=win["hwnd_hex"])
    assert got is not None and got["hwnd"] == win["hwnd"]
    assert eye.resolve_window(hwnd=0x1) is None, "无效句柄应返回 None"


@test
def test_resolve_never_picks_self():
    """没有选择器时不能抓到自己 / DSH（交接文档 §4 手测最后一条）。"""
    win = eye.resolve_window()
    assert win is None or win.get("hwnd"), "resolve 返回了畸形结果"
    if win:
        assert "DeepSeek Harness" not in win["title"]
        assert win["pid"] != eye._SELF_PID


@test
def test_query_aliases():
    for q in ("browser", "editor", "terminal"):
        got = eye.resolve_window(query=q)
        assert got is None or got.get("ambiguous") or got["hwnd"] > 0
    game = eye.resolve_window(query="game")
    assert game is None or game.get("ambiguous"), "game 不许猜，必须给候选"
    try:
        eye.resolve_window(query="不存在的别名")
    except ValueError:
        pass
    else:
        raise AssertionError("未知别名应当抛 ValueError")


@test
def test_ambiguous_shape():
    cands = eye.list_windows()
    if len(cands) < 2:
        return
    got = eye._ambiguous(cands, "unit")
    assert got["ambiguous"] is True and got["candidates"]
    assert len(got["candidates"]) <= 10


# ---------------------------------------------------------------- 捕获

@test
def test_capture_window():
    """截一个自建的测试窗口，并把证据写进**临时**仓库，不污染 ~/.ai-eyes。"""
    saved = (evidence.CAPTURE_DIR, evidence.INDEX, evidence.LATEST)
    with tempfile.TemporaryDirectory(prefix="eyes_capture_") as tmp:
        home = Path(tmp)
        evidence.CAPTURE_DIR = home / "captures"
        evidence.INDEX = home / "index.jsonl"
        evidence.LATEST = home / "latest.json"
        try:
            with _FixtureWindow() as win:
                img = _capture_ready(win["hwnd"])
                assert img is not None, "测试窗口截图失败"
                assert img.width <= 1280, "超过 max_width"
                assert _mean(img) > 5, "截图是黑的（mean=%.2f）" % _mean(img)

                record = evidence.capture(img, win, trigger="test", source="window", locked=True)
                for key in ("capture_id", "ts", "seq", "window_seq", "trigger", "source",
                            "locked", "window", "image"):
                    assert key in record, "证据缺字段 %s" % key
                assert record["capture_id"].startswith("eye_")
                assert record["window"]["process"].lower().startswith("python")
                assert record["window"]["title"] == FIXTURE_TITLE
                assert record["window"]["hwnd"] == win["hwnd_hex"]
                assert record["image"]["width"] <= 1280
                assert record["image"]["height"] > 0

                png = Path(record["image"]["path"])
                assert png.exists()
                assert png.stat().st_size == record["image"]["bytes"]
                assert hashlib.sha256(png.read_bytes()).hexdigest() == record["image"]["sha256"]

                latest = evidence.latest()
                assert latest is not None and latest["capture_id"] == record["capture_id"]
        finally:
            evidence.CAPTURE_DIR, evidence.INDEX, evidence.LATEST = saved


@test
def test_minimized_returns_none():
    with _FixtureWindow(title="EYES_TEST_MINIMIZED") as win:
        hwnd = win["hwnd"]
        assert _capture_ready(hwnd) is not None       # 先确认正常
        eye.user32.ShowWindow(hwnd, 6)                # SW_MINIMIZE
        try:
            assert _wait(lambda: bool(eye.user32.IsIconic(hwnd)), 5), "最小化没生效"
            assert eye.capture_window(hwnd) is None, "最小化时应当返回 None，而不是黑图"
        finally:
            eye.user32.ShowWindow(hwnd, 9)             # SW_RESTORE


@test
def test_capture_minimized_with_restore():
    """最小化窗口：默认返回 None；显式 restore_minimized 时能截，且截完放回最小化。"""
    with _FixtureWindow(title="EYES_TEST_RESTORE") as win:
        hwnd = win["hwnd"]
        assert _capture_ready(hwnd) is not None
        eye.user32.ShowWindow(hwnd, 6)                # SW_MINIMIZE
        try:
            assert _wait(lambda: bool(eye.user32.IsIconic(hwnd)), 5), "最小化没生效"
            assert eye.capture_window(hwnd) is None, "默认不该截最小化窗口"
            img = eye.capture_window(hwnd, restore_minimized=True)
            assert img is not None, "不激活还原后仍截不到"
            assert _mean(img) > 5, "还原后截到的是黑图"
            assert eye.user32.IsIconic(hwnd), "截完应当放回最小化"
        finally:
            eye.user32.ShowWindow(hwnd, 9)             # SW_RESTORE


@test
def test_crop_and_scaling():
    """--crop 只截局部；--max-width 是等比缩放而不是裁掉右边。"""
    with _FixtureWindow(title="EYES_TEST_CROP") as win:
        hwnd = win["hwnd"]
        full = _capture_ready(hwnd)
        assert full is not None

        img = eye.capture_window(hwnd, crop=(10, 10, 200, 150))
        assert img is not None and img.size == (200, 150), img.size

        small = eye.capture_window(hwnd, max_width=200)
        assert small is not None and small.width == 200
        expect_h = round(full.height * 200 / full.width)
        assert abs(small.height - expect_h) <= 2, (small.size, full.size)

        assert eye.looks_blank(Image.new("RGB", (300, 200), (255, 255, 255)))
        assert not eye.looks_blank(full)


@test
def test_cli_exit_codes():
    code, wins = _cli("list", "--json")
    assert code == 0 and isinstance(wins, list) and wins

    code, out = _cli("capture", "--hwnd", "0x1")
    assert code == 2 and isinstance(out, dict) and out.get("reason") == "window_not_found"

    code, out = _cli("capture", "--process", "no-such-process-xyz")
    assert code == 2

    code, out = _cli("capture", "--process", "no-such-process-xyz", "--max-width", "0")
    assert code == 4, "参数错误应当 exit 4，实际 %s" % code

    code, out = _cli("capture", "--crop", "1,2,3")
    assert code == 4, "--crop 格式错误应当 exit 4，实际 %s" % code

    code, out = _cli("latest", "--json")
    assert code == 0 and isinstance(out, dict) and out.get("capture_id")


# ---------------------------------------------------------------- 证据层

@test
def test_doctor():
    code, out = _cli("doctor", "--json")
    assert code == 0, "doctor 应当 exit 0，实际 %s" % code
    assert isinstance(out, dict) and out.get("ok") is True
    names = {c["name"] for c in out["checks"]}
    assert {"python", "pillow", "evidence_dir", "windows"} <= names, names


@test
def test_capture_ids_sortable():
    a = evidence.new_capture_id()
    time.sleep(0.005)
    b = evidence.new_capture_id()
    assert a < b, "capture_id 必须可按字符串排序（ULID 时间前缀）"
    assert a.startswith("eye_") and len(a) == 30


@test
def test_evidence_seq_and_retention():
    saved = (evidence.CAPTURE_DIR, evidence.INDEX, evidence.LATEST)
    try:
        with tempfile.TemporaryDirectory(prefix="eyes_evidence_") as tmp:
            home = Path(tmp)
            evidence.CAPTURE_DIR = home / "captures"
            evidence.INDEX = home / "index.jsonl"
            evidence.LATEST = home / "latest.json"
            win = {"hwnd": 0x1234, "title": "t", "process": "p.exe",
                   "pid": 1, "class_name": "c"}
            img = Image.new("RGB", (64, 48), (200, 30, 30))

            r1 = evidence.capture(img, win)
            r2 = evidence.capture(img, win)
            assert r1["seq"] == 1 and r2["seq"] == 2, "seq 必须全局递增"
            assert r1["window_seq"] == 1 and r2["window_seq"] == 2, "window_seq 按 hwnd 递增"
            assert evidence.latest()["capture_id"] == r2["capture_id"]
            assert evidence.find(r1["capture_id"]) == r1
            assert len(evidence.read_index()) == 2

            evidence._prune(keep=1)                    # 只留最新一张
            assert Path(r2["image"]["path"]).exists()
            assert not Path(r1["image"]["path"]).exists()
            assert len(evidence.read_index()) == 2, "prune 不许删 index"
    finally:
        evidence.CAPTURE_DIR, evidence.INDEX, evidence.LATEST = saved


# ---------------------------------------------------------------- 入口

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
