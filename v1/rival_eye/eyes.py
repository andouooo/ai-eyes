"""AI 的眼睛 · v1 视觉证据层：按名字 / 进程 / 句柄找窗口，PrintWindow 截一张可审计的图。

三条边界（与交接文档一致）：
- 只读：不改目标窗口、不抢焦点、不注入按键、不注册热键；
- 只看：不联网、不上传，证据只落在本机 ``~/.ai-eyes``；
- 只做窗口：网页内容优先交给 browser-skill（DOM / 控制台 / 网络更可靠）。

本模块刻意不 import ``rival_eye.assistant.winrelay``：交接文档要求“只能复制，不改原函数”，
自包含一份 ctypes 声明可以避免把热键、UI、回读那些链路拖进来，也不会互相影响。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    # 按文件路径直接运行（python ...\v1\rival_eye\eyes.py）时，把包根塞进 sys.path。
    # 这样 skill 里可以写绝对路径调用，不依赖“必须先 cd 到 v1”。
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.platform != "win32":  # pragma: no cover - 本项目只面向 Windows
    raise ImportError("rival_eye.eyes 仅支持 Windows")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ---------------------------------------------------------------- Win32 常量

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PW_RENDERFULLCONTENT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 噪声窗口：桌面宿主、输入法候选框等，永远不参与选择
_NOISE_TITLES = {
    "Program Manager",
    "Windows Input Experience",
    "Windows 输入体验",
    "MSCTFIME UI",
    "Default IME",
}

# 默认排除的进程：自己（python / DSH）与各种终端。
# 不排除的话，“看一眼浏览器”会抓到命令行，演示当场翻车。
_DEFAULT_EXCLUDED_PROCESSES = {
    "python.exe",
    "pythonw.exe",
    "windowsterminal.exe",
    "openconsole.exe",
    "conhost.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "wt.exe",
}

# DSH 自身的窗口标题
_DEFAULT_EXCLUDED_TITLES = ("DeepSeek Harness",)

# 敏感窗口：密码管理器 / 网银。SKILL.md 里写了“拒绝”，这里再加一道代码防线，
# 免得换个 agent 或手滑就直接截了。确需截图时用 --allow-sensitive 显式放行。
_SENSITIVE_KEYWORDS = (
    "1password", "bitwarden", "keepass", "lastpass", "enpass", "dashlane",
    "密码", "密钥", "网银", "网上银行", "支付密码", "wallet", "keystore",
)

# query 别名（大小写不敏感）；game 故意不猜
QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "browser": ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"),
    "浏览器": ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"),
    "editor": ("code.exe", "cursor.exe", "notepad.exe"),
    "编辑器": ("code.exe", "cursor.exe", "notepad.exe"),
    "terminal": ("windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
                 "conhost.exe", "openconsole.exe"),
    "终端": ("windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
             "conhost.exe", "openconsole.exe"),
}

_SELF_PID = os.getpid()

# ---------------------------------------------------------------- ctypes 声明
#
# 64 位下句柄会被截断成 32 位，所有入参/返回值必须显式声明（交接文档 §5.2）。

kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.CloseHandle.restype = w.BOOL
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.QueryFullProcessImageNameW.restype = w.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, w.DWORD,
                                                w.LPWSTR, ctypes.POINTER(w.DWORD)]

user32.IsWindow.restype = w.BOOL
user32.IsWindow.argtypes = [w.HWND]
user32.IsWindowVisible.restype = w.BOOL
user32.IsWindowVisible.argtypes = [w.HWND]
user32.IsIconic.restype = w.BOOL
user32.IsIconic.argtypes = [w.HWND]
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [w.HWND]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetWindowLongW.restype = w.LONG
user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
user32.GetWindowRect.restype = w.BOOL
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.EnumWindows.restype = w.BOOL
user32.GetWindowDC.restype = ctypes.c_void_p
user32.GetWindowDC.argtypes = [w.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [w.HWND, ctypes.c_void_p]
user32.PrintWindow.restype = w.BOOL
user32.PrintWindow.argtypes = [w.HWND, ctypes.c_void_p, w.UINT]

gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.restype = w.BOOL
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.restype = w.BOOL
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.UINT, w.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, w.UINT]

_dwm = getattr(ctypes.windll, "dwmapi", None)
if _dwm is not None:
    _dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    _dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]


class _BMIH(ctypes.Structure):
    """BITMAPINFOHEADER（只用到前 40 字节）。"""
    _fields_ = [
        ("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD),
    ]


class _DPI:
    """进程级 DPI 感知。只做一次，失败就算了（老系统没有这些 API）。"""
    done = False
    ok = False

    @classmethod
    def enable(cls) -> None:
        if cls.done:
            return
        cls.done = True
        try:  # Win10 1703+：PER_MONITOR_AWARE_V2
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                cls.ok = True
                return
        except Exception:
            pass
        try:  # Win8.1+
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
                cls.ok = True
            return
        except Exception:
            pass
        try:  # Vista+
            user32.SetProcessDPIAware()
            cls.ok = True
        except Exception:
            pass


# ---------------------------------------------------------------- 窗口信息

def _window_text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    n = user32.GetClassNameW(hwnd, buf, 256)
    return buf.value if n > 0 else ""


def _pid_of(hwnd) -> int:
    pid = w.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_name(pid: int) -> str:
    """pid → 可执行文件名（chrome.exe）。拿不到就返回空串，不抛异常。"""
    if pid <= 0:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
    finally:
        kernel32.CloseHandle(h)
    return ""


def _window_bounds(hwnd) -> tuple[int, int, int, int]:
    """可见边框（DWM 扩展边框优先，去掉 Win10 的不可见阴影边距）。"""
    rect = w.RECT()
    if _dwm is not None:
        try:
            hr = _dwm.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                            ctypes.byref(rect), ctypes.sizeof(rect))
            if hr == 0 and rect.right > rect.left and rect.bottom > rect.top:
                return (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            pass
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (rect.left, rect.top, rect.right, rect.bottom)
    return (0, 0, 0, 0)


def _is_cloaked(hwnd) -> bool:
    """UWP / 虚拟桌面里“存在但不可见”的窗口，枚举时会出现，必须滤掉。"""
    if _dwm is None:
        return False
    val = ctypes.c_int(0)
    try:
        hr = _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED,
                                        ctypes.byref(val), ctypes.sizeof(val))
        return hr == 0 and val.value != 0
    except Exception:
        return False


def _describe(hwnd, *, foreground=None) -> dict | None:
    """把 hwnd 变成窗口 dict；不是可见顶层窗口就返回 None。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    if not user32.IsWindowVisible(hwnd):
        return None
    if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
        return None
    if _is_cloaked(hwnd):
        return None
    title = _window_text(hwnd).strip()
    if not title or title in _NOISE_TITLES:
        return None
    left, top, right, bottom = _window_bounds(hwnd)
    if right <= left or bottom <= top:
        return None
    pid = _pid_of(hwnd)
    return {
        "hwnd": int(hwnd),
        "hwnd_hex": "0x%08X" % int(hwnd),
        "title": title,
        "pid": pid,
        "process": _process_name(pid),
        "class_name": _class_name(hwnd),
        "bounds": [left, top, right, bottom],
        "is_foreground": bool(hwnd == (foreground or user32.GetForegroundWindow())),
        "is_minimized": bool(user32.IsIconic(hwnd)),
    }


def _enumerate_all() -> list[dict]:
    """按 Z 序（前台在前）返回所有可见顶层窗口，未做任何排除。"""
    out: list[dict] = []
    fg = user32.GetForegroundWindow()

    def _cb(hwnd, _lparam):
        info = _describe(hwnd, foreground=fg)
        if info is not None:
            out.append(info)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return out


def _default_skip_reason(win: dict, *, skip_processes: set[str]) -> str | None:
    """命中默认排除就返回原因；否则 None。"""
    proc = win["process"].lower()
    if proc in skip_processes:
        return "process:%s" % proc
    for bad in _DEFAULT_EXCLUDED_TITLES:
        if bad.lower() in win["title"].lower():
            return "title:%s" % bad
    return None


def list_windows(exclude_titles=(), exclude_processes=(), *,
                 apply_defaults: bool = True,
                 include: set[str] | None = None) -> list[dict]:
    """按 Z 序返回可见顶层窗口（前台在前）。

    ``apply_defaults`` 为真时套用默认排除（自身 / python / 终端 / DSH）；
    ``include`` 里列出的进程即使命中默认排除也放行（query=终端 时要用）。
    """
    include = {p.lower() for p in (include or ())}
    extra_proc = {p.lower() for p in exclude_processes if p}
    extra_title = tuple(t for t in exclude_titles if t)
    skip = set(_DEFAULT_EXCLUDED_PROCESSES) - include
    skip |= extra_proc

    result = []
    for win in _enumerate_all():
        # 自己的窗口永远不列：不然“看一眼浏览器”会抓到 DSH / 命令行自己
        if win["pid"] == _SELF_PID:
            continue
        if apply_defaults and _default_skip_reason(win, skip_processes=skip):
            continue
        if any(t.lower() in win["title"].lower() for t in extra_title):
            continue
        result.append(win)
    return result


# ---------------------------------------------------------------- 选择窗口

def _as_hwnd(value) -> int:
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        raise ValueError("hwnd 为空")
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s, 10)
    except ValueError:
        raise ValueError("hwnd 不是合法句柄：%r" % value) from None


def _titles_differ(a: str, b: str) -> bool:
    """两个候选的标题是否“差得很大”（同应用不同文档算像，不同应用算差）。"""
    import difflib

    def stem(t: str) -> str:
        for sep in (" - ", " — ", " | "):
            if sep in t:
                return t.split(sep)[0].strip()
        return t.strip()

    sa, sb = stem(a).lower(), stem(b).lower()
    if sa == sb:
        return False
    return difflib.SequenceMatcher(None, sa, sb).ratio() < 0.5


def _is_sensitive(win: dict) -> bool:
    """密码管理器 / 网银之类的窗口，默认拒绝截图。"""
    blob = ("%s %s" % (win.get("process", ""), win.get("title", ""))).lower()
    return any(k in blob for k in _SENSITIVE_KEYWORDS)


def _ambiguous(cands: list[dict], reason: str) -> dict:
    return {
        "ambiguous": True,
        "reason": reason,
        "candidates": cands[:10],
    }


def resolve_window(*, hwnd=None, process=None, title=None, query=None) -> dict | None:
    """按句柄 / 进程 / 标题 / 别名找窗口。

    返回窗口 dict；找不到返回 None；多个候选无法自动判断时返回
    ``{"ambiguous": True, "candidates": [...]}``，调用方应当问用户。
    """
    if hwnd is not None:
        info = _describe(_as_hwnd(hwnd))
        return info

    wins = _enumerate_all()

    if query:
        key = str(query).strip().lower()
        if key in ("game", "游戏"):
            # 交接文档：游戏不猜，返回候选让调用方选
            return _ambiguous(list_windows(), "game_never_guesses")
        aliases = QUERY_ALIASES.get(key)
        if aliases is None:
            raise ValueError("未知 query：%r（可用：browser/editor/terminal/game 或中文）"
                             % query)
        cands = [x for x in wins if x["process"].lower() in aliases and x["pid"] != _SELF_PID]
        return _choose(cands, "query:%s" % key)

    def match(win: dict) -> bool:
        if process:
            want = str(process).strip().lower()
            proc = win["process"].lower()
            if want not in proc and want not in ("",):
                return False
        if title:
            if str(title).strip().lower() not in win["title"].lower():
                return False
        return True

    if process or title:
        return _choose([x for x in wins if match(x)], "selector")

    # 没有选择器：给“最该看”的那个窗口——前台优先，但要跳过自身 / 终端 / DSH，
    # 否则在命令行里问“看一眼”只会看到自己（交接文档 §4 手测最后一条）。
    cands = list_windows()
    if not cands:
        return None
    fg = [x for x in cands if x["is_foreground"]]
    return fg[0] if fg else cands[0]


def _choose(cands: list[dict], reason: str) -> dict | None:
    if not cands:
        return None
    fg = [c for c in cands if c["is_foreground"]]
    if len(fg) == 1:
        return fg[0]
    top = cands[0]                       # EnumWindows 的次序就是 Z 序
    if len(cands) > 1 and _titles_differ(top["title"], cands[1]["title"]):
        return _ambiguous(cands, reason)
    return top


# ---------------------------------------------------------------- 捕获

SW_SHOWNOACTIVATE = 4
SW_MINIMIZE = 6


def capture_window(hwnd, max_width: int = 1280, restore_minimized: bool = False,
                   crop=None):
    """PrintWindow 捕获窗口内容（被遮挡也能截到，Chromium/Electron 有效）。

    最小化 / 黑屏 / 无法渲染都返回 None；返回 PIL RGB 图。

    - ``max_width``：按原始尺寸抓完后等比缩小（**不是裁剪**）。
    - ``crop=(x, y, w, h)``：在缩之前的原图上裁一块（省 token 用）。
    - ``restore_minimized=True``：最小化窗口系统不再渲染，像素根本不存在。这时先
      “不激活地还原”（SW_SHOWNOACTIVATE，抢不到焦点），截完再放回最小化。代价是
      窗口会在屏幕上闪一下，托盘类应用可能被拉出来——所以默认关闭，由调用方显式开启。
    """
    hwnd = _as_hwnd(hwnd)
    if not user32.IsWindow(hwnd):
        return None

    restored = False
    if user32.IsIconic(hwnd):
        if not restore_minimized:
            return None
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        time.sleep(0.45)                     # 等一帧渲染出来
        restored = True

    try:
        return _grab(hwnd, max_width, crop=crop)
    finally:
        if restored and not user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_MINIMIZE)


def _grab(hwnd, max_width: int, crop=None):
    from PIL import Image, ImageStat

    left, top, right, bottom = _window_bounds(hwnd)
    wdt, hgt = right - left, bottom - top
    if wdt <= 0 or hgt <= 0:
        return None

    # 必须按窗口原始尺寸建位图：PrintWindow 不会替你缩放，DC 小了只会把右边/下边裁掉
    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, wdt, hgt)
    img = None
    try:
        gdi32.SelectObject(mem_dc, bmp)
        ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
        if not ok:
            ok = user32.PrintWindow(hwnd, mem_dc, 0)
        if not ok:
            return None
        header = _BMIH()
        header.biSize = ctypes.sizeof(header)
        header.biWidth, header.biHeight = wdt, -hgt      # 负高 = top-down
        header.biPlanes, header.biBitCount, header.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(wdt * hgt * 4)
        if not gdi32.GetDIBits(mem_dc, bmp, 0, hgt, buf, ctypes.byref(header), 0):
            return None
        raw = Image.frombytes("RGBA", (wdt, hgt), buf.raw)   # 内存布局实为 BGRA
        b, g, r, _a = raw.split()
        img = Image.merge("RGB", (r, g, b))
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

    if crop:
        x, y, cw, ch = (int(v) for v in crop)
        x = max(0, min(x, img.width - 1))
        y = max(0, min(y, img.height - 1))
        cw = max(1, min(cw, img.width - x))
        ch = max(1, min(ch, img.height - y))
        img = img.crop((x, y, x + cw, y + ch))

    if max_width and img.width > max_width:
        img = img.resize((max_width, max(1, int(round(img.height * max_width / img.width)))),
                         Image.Resampling.BILINEAR)
    # 全黑 = 没截到（独占全屏 / GPU 独占 / 窗口刚销毁），当失败处理，别把黑图当证据
    if ImageStat.Stat(img.convert("L")).mean[0] < 5:
        return None
    return img


def looks_blank(img) -> bool:
    """近似“整块纯色”：Chromium 被完全遮挡时可能只画出框架、内容区一片白。

    只当提醒，不当失败——空白的记事本、纯色画布也可能真的长这样。
    """
    from PIL import ImageStat

    if img is None or img.width * img.height < 20000:
        return False
    return ImageStat.Stat(img.convert("L")).stddev[0] < 2.0


# ---------------------------------------------------------------- CLI

class _Parser(argparse.ArgumentParser):
    """参数错误统一 exit 4（交接文档 §3.2），argparse 默认是 2。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        raise SystemExit(4)


def _emit(obj) -> None:
    # ensure_ascii=True：Windows 控制台代码页五花八门，转义后才不会打出乱码
    print(json.dumps(obj, ensure_ascii=True, indent=2))


def _fail(reason: str, message: str, code: int) -> int:
    sys.stderr.write("[eyes] %s: %s\n" % (reason, message))
    _emit({"ok": False, "reason": reason, "error": message})
    return code


def _cmd_list(args) -> int:
    include: set[str] = set()
    if args.query:
        key = args.query.strip().lower()
        if key in ("game", "游戏"):
            wins = list_windows()
            _emit(wins)
            return 0
        include = set(QUERY_ALIASES.get(key, ()))
        if not include:
            raise ValueError("未知 query：%r" % args.query)
    wins = list_windows(
        exclude_titles=args.exclude_title or (),
        apply_defaults=not args.all,
        include=include,
    )
    if args.query:
        wins = [x for x in wins if x["process"].lower() in include]
    if args.process:
        want = args.process.strip().lower()
        wins = [x for x in wins if want in x["process"].lower()]
    if args.title:
        want = args.title.strip().lower()
        wins = [x for x in wins if want in x["title"].lower()]
    if args.limit and args.limit > 0:
        wins = wins[: args.limit]
    _emit(wins)
    return 0


def _cmd_capture(args) -> int:
    import rival_eye.evidence as evidence

    if args.max_width < 1:
        raise ValueError("--max-width 必须为正整数")

    selector = any([args.hwnd, args.process, args.title, args.query])
    win = resolve_window(hwnd=args.hwnd, process=args.process,
                         title=args.title, query=args.query)
    if win is None:
        return _fail("window_not_found", "找不到匹配的窗口，先用 list 看看有哪些", 2)
    if win.get("ambiguous"):
        _emit({"ok": False, "reason": "ambiguous",
               "error": "多个候选窗口，请让用户确认或显式指定 hwnd",
               "candidates": win["candidates"]})
        return 2

    if _is_sensitive(win) and not args.allow_sensitive:
        return _fail("sensitive",
                     "这是敏感窗口（密码管理器 / 网银 / 密钥），默认拒绝截图；"
                     "确需截图请加 --allow-sensitive", 5)

    hwnd = win["hwnd"]
    was_minimized = bool(user32.IsIconic(hwnd))
    img = capture_window(hwnd, max_width=args.max_width, crop=args.crop,
                         restore_minimized=bool(args.restore))
    if img is None:
        if was_minimized:
            if args.restore:
                return _fail("minimized", "窗口已最小化，且这次还原后仍然截不到（可能被托盘接管）", 3)
            return _fail("minimized",
                         "窗口已最小化；加 --restore 可先不激活地还原、截完放回", 3)
        if not user32.IsWindow(hwnd):
            return _fail("window_gone", "窗口已关闭", 3)
        return _fail("capture_failed",
                     "捕获失败：可能独占全屏 / GPU 独占，建议无边框窗口化", 3)

    extra = {}
    if was_minimized and args.restore:
        extra["restored"] = True
    if looks_blank(img):
        extra["blank_warning"] = True
    record = evidence.capture(img, win, trigger=args.trigger, source="window",
                              locked=bool(selector), extra=extra or None)
    if extra.get("restored"):
        sys.stderr.write("[eyes] 窗口曾最小化：已临时还原截图并放回（不抢焦点）\n")
    if extra.get("blank_warning"):
        sys.stderr.write("[eyes] 警告：整张图近乎纯色，可能是 Chromium 被遮挡只画了框架；"
                         "建议加 --restore 或让窗口露出来一点再截\n")
    _emit(record)
    return 0


def _cmd_latest(args) -> int:
    import rival_eye.evidence as evidence

    rec = evidence.latest()
    if rec is None:
        return _fail("no_capture", "还没有任何截图证据", 2)
    _emit(rec)
    return 0


def _cmd_doctor(args) -> int:
    """现场演示前的自检：依赖、证据目录、窗口枚举、OCR、测试夹具。"""
    import platform
    import tempfile

    import rival_eye.evidence as evidence

    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    add("python", sys.version_info >= (3, 9), platform.python_version())
    try:
        from PIL import Image
        add("pillow", True, getattr(Image, "__version__", "?"))
    except Exception as exc:
        add("pillow", False, exc)

    try:
        evidence._ensure_dirs()
        probe = Path(evidence.AI_EYES_HOME) / ".doctor.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("evidence_dir", True, evidence.AI_EYES_HOME)
    except Exception as exc:
        add("evidence_dir", False, exc)

    try:
        wins = list_windows()
        add("windows", bool(wins), "%d 个可截窗口" % len(wins))
    except Exception as exc:
        add("windows", False, exc)

    try:
        import winocr  # noqa: F401
        add("ocr", True, "winocr 已装（--ocr 可用）")
    except Exception:
        add("ocr", False, "未装 winocr（--ocr 才会用到，可选）")

    try:
        import tkinter  # noqa: F401
        add("tkinter", True, "测试夹具可用")
    except Exception:
        add("tkinter", False, "未装 tkinter（跑 tests 需要）")

    add("dpi", _DPI.ok, "per-monitor v2" if _DPI.ok else "未启用，缩放 >100% 时截图会错位")

    latest = None
    rec = evidence.latest()
    if rec:
        latest = {
            "capture_id": rec.get("capture_id"),
            "ts": rec.get("ts"),
            "process": (rec.get("window") or {}).get("process"),
            "title": (rec.get("window") or {}).get("title"),
        }

    required = ("python", "pillow", "evidence_dir", "windows")
    ok = all(c["ok"] for c in checks if c["name"] in required)
    _emit({"ok": ok, "checks": checks, "latest": latest})
    return 0 if ok else 1


def _crop_arg(value):
    """--crop x,y,w,h（原图像素，缩放前）。"""
    try:
        parts = [int(x) for x in str(value).replace(" ", "").split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("--crop 需要 x,y,w,h 四个整数") from None
    if len(parts) != 4 or parts[0] < 0 or parts[1] < 0 or parts[2] <= 0 or parts[3] <= 0:
        raise argparse.ArgumentTypeError("--crop 需要 x,y,w,h（w/h > 0，x/y >= 0）")
    return tuple(parts)


def build_parser() -> _Parser:
    p = _Parser(prog="python -m rival_eye.eyes",
                description="AI 的眼睛：按名字/进程/句柄捕获窗口，产出带 capture_id 的截图证据。")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _target(sp):
        sp.add_argument("--hwnd", help="窗口句柄，支持 0x 前缀")
        sp.add_argument("--process", help="进程名，可只写前缀（chrome）")
        sp.add_argument("--title", help="标题子串")
        sp.add_argument("--query", help="别名：browser/editor/terminal/game 或中文")

    lst = sub.add_parser("list", help="列出可见窗口（Z 序）")
    _target(lst)
    lst.add_argument("--all", action="store_true", help="包含默认排除的窗口（终端/python）")
    lst.add_argument("--exclude-title", action="append", help="额外按标题排除")
    lst.add_argument("--limit", type=int, default=0, help="最多返回条数")
    lst.add_argument("--json", action="store_true", help="兼容参数，输出本来就是 JSON")

    cap = sub.add_parser("capture", help="截一张图并写入证据")
    _target(cap)
    cap.add_argument("--max-width", type=int, default=1280, help="最长边像素（默认 1280）")
    cap.add_argument("--crop", type=_crop_arg, metavar="X,Y,W,H",
                     help="只截一块（原图像素，缩放前），省 token 用")
    cap.add_argument("--restore", action="store_true",
                     help="窗口最小化时先不激活地还原、截完放回（会闪一下，但不抢焦点）")
    cap.add_argument("--allow-sensitive", action="store_true",
                     help="允许截密码管理器 / 网银这类敏感窗口（默认拒绝）")
    cap.add_argument("--trigger", default="agent", help="触发者标记（默认 agent）")
    cap.add_argument("--json", action="store_true", help="兼容参数，输出本来就是 JSON")

    lat = sub.add_parser("latest", help="打印最近一条证据")
    lat.add_argument("--json", action="store_true", help="兼容参数，输出本来就是 JSON")

    doc = sub.add_parser("doctor", help="自检：依赖 / 证据目录 / 窗口枚举 / OCR / 测试夹具")
    doc.add_argument("--json", action="store_true", help="兼容参数，输出本来就是 JSON")

    return p


def main(argv=None) -> int:
    _DPI.enable()
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.cmd == "list":
            return _cmd_list(args)
        if args.cmd == "capture":
            return _cmd_capture(args)
        if args.cmd == "latest":
            return _cmd_latest(args)
        if args.cmd == "doctor":
            return _cmd_doctor(args)
        raise ValueError("未知子命令：%r" % args.cmd)
    except ValueError as exc:
        return _fail("bad_args", str(exc), 4)


if __name__ == "__main__":
    raise SystemExit(main())
