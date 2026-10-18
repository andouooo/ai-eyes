"""命令行入口：
  demo       离线演示：内置示例页面 → 截图 → 语义diff → 红框报告
  shot       对任意 URL 截图
  diff       对比两张已有截图并生成报告
  watch      巡检一个 URL（重复执行即与上次基线增量对比）
  assistant  桌面悬浮窗助手：点击提问，AI 看你的屏幕作答
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

from . import capture, diff, notify
from . import report as report_mod
from .config import DEMO_DIR, SNAP_DIR


def _open_report(html: Path):
    """自动用浏览器打开报告。

    snap 打包的浏览器（Ubuntu 默认）禁止读取隐藏目录（如 ~/.zcode/...），
    报告若藏在隐藏路径，先镜像到 /tmp 可见位置再打开。
    """
    import shutil
    import tempfile
    import webbrowser

    try:
        resolved = html.resolve()
        in_hidden = any(part.startswith(".") for part in resolved.parts)
        if not in_hidden:
            webbrowser.open(resolved.as_uri())
            return
        staging = Path(tempfile.mkdtemp(prefix="rivaleye_report_"))
        shutil.copy2(resolved, staging / resolved.name)
        try:
            shutil.copytree(resolved.parent.parent / "snapshots", staging / "snapshots",
                            dirs_exist_ok=True)
        except Exception:
            pass
        webbrowser.open((staging / resolved.name).as_uri())
    except Exception:
        pass


def _run_pair(png_a: Path, png_b: Path, title: str):
    d = diff.semantic_diff(png_a, png_b)
    html, md = report_mod.build(d, png_a, png_b, title)
    notify.push(f"【竞眼雷达】{d.get('summary', '')}\n报告：{html}")
    if getattr(sys, "frozen", False):  # 打包版没有控制台，生成报告后直接打开
        _open_report(html)
    return d, html, md


def cmd_demo(args):
    if getattr(sys, "frozen", False):
        _notify("巡检进行中：截图与 AI 分析约需 20 秒，完成后自动打开报告")
    site = DEMO_DIR / "site"
    a = SNAP_DIR / "demo_day1.png"
    b = SNAP_DIR / "demo_day2.png"
    print("① 截图：昨天 / 今天 …")
    capture.shot((site / "day1.html").resolve().as_uri(), a)
    capture.shot((site / "day2.html").resolve().as_uri(), b)
    print("② VLM 语义对比 …")
    d, html, md = _run_pair(a, b, "竞眼雷达 · 演示：CompetitorX 官网 24h 变化")
    print(json.dumps(d, ensure_ascii=False, indent=2))
    print(f"\n✅ HTML 报告：{html}\n✅ Markdown 摘要：{md}")


def cmd_shot(args):
    print(capture.shot(args.url, Path(args.out)))


def cmd_diff(args):
    d, html, md = _run_pair(Path(args.a), Path(args.b), args.title)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    print(f"\n✅ HTML 报告：{html}")


def cmd_watch(args):
    baseline = SNAP_DIR / "watch_baseline.png"
    if not baseline.exists():
        print("首轮巡检：建立基线快照（下次执行将与之对比）")
        print(capture.shot(args.url, baseline))
        return
    cur = SNAP_DIR / f"watch_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
    capture.shot(args.url, cur)
    d, html, md = _run_pair(baseline, cur, f"竞眼雷达 · 巡检 {args.url}")
    print(d.get("summary", ""))
    print(f"✅ HTML 报告：{html}")
    baseline.unlink()
    cur.rename(baseline)


def _notify(text):
    """桌面通知（libnotify）。失败静默，只做启动反馈用。"""
    try:
        import subprocess

        subprocess.run(
            ["notify-send", "竞眼雷达", text],
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _has_gui_backend() -> bool:
    try:
        import PySide6  # noqa: F401

        return True
    except ImportError:
        pass
    try:
        import tkinter  # noqa: F401

        return True
    except ImportError:
        return False


def cmd_assistant(args):
    if sys.platform == "win32":
        from .assistant import winrelay

        if not winrelay.acquire_single_instance():
            winrelay.broadcast_show_console()  # 已有实例：唤醒其控制台，自己静默退出
            return

    try:
        from .assistant.agent import AssistantAgent

        agent = AssistantAgent()
    except ImportError as e:
        raise SystemExit(f"缺少依赖（{e}），请先 pip install -r requirements.txt")

    last_err = None
    try:
        from .assistant.overlay_qt import OverlayApp  # Qt 后端（优先，无系统依赖）

        OverlayApp(agent).run()
        return
    except ImportError as e:
        last_err = e
    try:
        from .assistant.overlay import OverlayApp  # Tk 后备

        OverlayApp(agent).run()
        return
    except ImportError:
        raise SystemExit(f"无可用 GUI 后端（{last_err}）；请 pip install PySide6 或使用带 tkinter 的 Python")


def main():
    p = argparse.ArgumentParser(prog="rival-eye", description="竞眼雷达 — AI 竞品视觉雷达")
    sub = p.add_subparsers(required=False)  # 无子命令 = 双击默认行为（见下方兜底）

    s = sub.add_parser("demo", help="离线演示：内置示例页面 → 截图 → diff → 报告")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("shot", help="对任意 URL 截图")
    s.add_argument("url")
    s.add_argument("out")
    s.set_defaults(func=cmd_shot)

    s = sub.add_parser("diff", help="对比两张截图并生成报告")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--title", default="竞眼雷达 · 巡检报告")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("watch", help="巡检一个 URL（重复执行即增量对比）")
    s.add_argument("url")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("assistant", help="桌面悬浮窗助手：点击提问，AI 看你的屏幕作答")
    s.set_defaults(func=cmd_assistant)

    args, _extra = p.parse_known_args()  # 容忍桌面启动器可能塞进来的未知参数
    if not getattr(args, "func", None):
        # 不带子命令（打包后双击）：有 GUI 后端 → 悬浮窗助手；否则离线演示并弹通知
        if _has_gui_backend():
            args = p.parse_args(["assistant"])
        else:
            _notify("未检测到 GUI 后端，进入离线雷达演示，报告完成后自动打开")
            args = p.parse_args(["demo"])
    args.func(args)


if __name__ == "__main__":
    main()
