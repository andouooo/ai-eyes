"""代码守卫：拦截锁定窗口（编辑器）里的回车，做行级语法体检。

链路：WH_KEYBOARD_LL 低级钩子（只拦真实回车、前台=锁定窗口、功能开启且不在豁免期）
→ 注入 Home/Shift+End/Ctrl+C 取当前行（注入键自带豁免，不会二次触发钩子）
→ check_line 规则检查 → 硬错误弹气泡拦截，软问题提示后放行，干净则注入回车无感放行。

设计红线：钩子回调内绝不做慢操作（只做标志位判断）；任何异常都放行按键，
宁可漏拦不可卡死键盘；3 秒豁免期保证用户随时能强制换行。
"""
import ctypes
import ctypes.wintypes as w
import html
import re
import threading
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
LLKHF_INJECTED = 0x10
VK_RETURN = 0x0D
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, w.WPARAM, w.LPARAM]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, w.DWORD]


class LowLevelKeyboardHook:
    """全局低级键盘钩子：满足 should_block() 时吞掉回车并回调 on_enter()。"""

    def __init__(self, on_enter, should_block):
        self._on_enter = on_enter
        self._should_block = should_block
        self._hook = None
        self._tid = 0
        self._stopped = threading.Event()

    def _proc(self, ncode, wparam, lparam):
        if ncode == 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            try:
                kb = KBDLLHOOKSTRUCT.from_address(lparam)
                if (kb.vkCode == VK_RETURN and not (kb.flags & LLKHF_INJECTED)
                        and self._should_block()):
                    self._on_enter()
                    return 1  # 吞掉本次回车
            except Exception:
                pass  # 钩子内绝不抛异常，异常时落入下方放行
        return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)

    def start(self) -> bool:
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
        self._proc_c = HOOKPROC(self._proc)  # 保持引用防 GC

        def run():
            self._tid = kernel32.GetCurrentThreadId()
            hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc_c, None, 0)
            if not hook:
                return
            self._hook = hook
            msg = w.MSG()
            while not self._stopped.is_set():
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):  # WM_QUIT / 错误
                    break
            user32.UnhookWindowsHookEx(hook)
            self._hook = None

        threading.Thread(target=run, daemon=True, name="guard-hook").start()
        return True

    def stop(self) -> None:
        self._stopped.set()
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


class ComboHook:
    """全局组合键（低级键盘钩子）——游戏里也能触发。

    为什么不用 RegisterHotKey：很多游戏（尤其全屏/独占、或用 raw input 的）会把系统热键吞掉，
    实测黑魂全屏时 Ctrl+Alt+B 到不了应用。低级键盘钩子是在系统分发之前看按键，游戏拦不住
    （除非游戏以管理员运行，那就属于 UIPI 的边界了）。
    设计红线：钩子回调里只做"按键匹配 + 回调"，绝不做慢操作；异常一律放行。
    """

    def __init__(self, vk: int, need_ctrl: bool, need_alt: bool, need_shift: bool, on_fire):
        self.vk = int(vk)
        self.need_ctrl = need_ctrl
        self.need_alt = need_alt
        self.need_shift = need_shift
        self._on_fire = on_fire
        self._hook = None
        self._tid = 0
        self._stopped = threading.Event()

    def _down(self, vk) -> bool:
        try:
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)
        except Exception:
            return False

    def _proc(self, ncode, wparam, lparam):
        try:
            if ncode == 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = KBDLLHOOKSTRUCT.from_address(lparam)
                # 不排除"注入键"：本程序自己只注入 Ctrl+V / 回车，从不注入这个组合，
                # 排除掉反而会让"用 SendInput 触发的场景"失效（自测也是靠它）。
                if kb.vkCode == self.vk:
                    if (self._down(0x11) == self.need_ctrl
                            and self._down(0x12) == self.need_alt
                            and self._down(0x10) == self.need_shift):
                        self._on_fire()
                        return 1          # 吞掉，避免游戏里也响应一次
        except Exception:
            pass
        return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)

    def start(self) -> bool:
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
        self._proc_c = HOOKPROC(self._proc)

        def run():
            self._tid = kernel32.GetCurrentThreadId()
            hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc_c, None, 0)
            if not hook:
                return
            self._hook = hook
            msg = w.MSG()
            while not self._stopped.is_set():
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
            user32.UnhookWindowsHookEx(hook)
            self._hook = None

        threading.Thread(target=run, daemon=True, name="combo-hook").start()
        return True

    def stop(self) -> None:
        self._stopped.set()
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


# ---------- 取当前行（注入按键 + 剪贴板，用完恢复） ----------

def read_current_line(expected_hwnd: int) -> str:
    """注入 Home/Shift+End/Ctrl+C 取当前行。前台若不是目标窗口立即放弃，绝不把按键打
    进别人的窗口；剪贴板用完恢复。"""
    from . import winrelay

    if not expected_hwnd or winrelay.user32.GetForegroundWindow() != expected_hwnd:
        return ""
    saved = winrelay.clipboard_text()
    try:
        time.sleep(0.25)  # 等目标窗口键盘焦点彻底就绪（刚激活时立即注入会丢键）
        winrelay.send_home()
        time.sleep(0.08)
        winrelay.send_shift_end()
        time.sleep(0.08)
        winrelay.send_ctrl_c()
        time.sleep(0.2)
        line = winrelay.clipboard_text()
        if winrelay.user32.GetForegroundWindow() == expected_hwnd:
            winrelay.send_end()  # 收起选区（前台还在才发）
    finally:
        if saved:
            winrelay.set_clipboard_text(saved)
    return line.splitlines()[0] if line.splitlines() else ""


# ---------- 行级语法检查（Python） ----------

FULLWIDTH = {"（": "(", "）": ")", "，": ",", "；": ";", "：": ":", "“": '"', "”": '"',
             "‘": "'", "’": "'", "！": "!", "【": "[", "】": "]", "｛": "{", "｝": "}"}
BRACKET_PAIR = {")": "(", "]": "[", "}": "{"}
STMT_KW = re.compile(r"^\s*(def|class|if|elif|else|for|while|try|except|finally|with)\b")
C_TYPE_KW = re.compile(r"^\s*(int|char|float|double|long|short|unsigned|signed|void|"
                       r"static|const|struct|union|enum|size_t|FILE)\b")
C_CTRL_KW = re.compile(r"^\s*(if|else|for|while|do|switch|case|default)\b")
C_OK_TAIL = (";", "{", "}", ",", "\\", "&&", "||", "+", "?", ":", "=", "(", "[",
             "&", "|", "<", ">")


def detect_lang(title: str) -> str:
    """从编辑器窗口标题识别语言（VSCode 标题含当前文件名，如 main.c）。"""
    m = re.search(r"[\w\-.]+\.(c|h|cpp|cc|cxx|py)\b", title or "")
    if not m:
        return "python"
    return "c" if m.group(1) != "py" else "python"


def _split_string_states(text, lang="python"):
    """逐字符扫描：返回 [(char, in_string, is_comment)]。

    python：# 行注释；c：// 行注释与块注释。字符串支持反斜杠转义。
    """
    out = []
    quote = None
    block = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if block:  # 块注释（c）
            if ch == "*" and nxt == "/":
                out.append((ch, False, True))
                out.append((nxt, False, True))
                i += 2
                block = False
                continue
            out.append((ch, False, True))
            i += 1
            continue
        if quote:
            out.append((ch, True, False))
            if ch == "\\" and nxt:
                out.append((nxt, True, False))
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if lang == "python" and ch == "#":
            out.extend((c, False, True) for c in text[i:])
            break
        if lang == "c" and ch == "/" and nxt == "/":
            out.extend((c, False, True) for c in text[i:])
            break
        if lang == "c" and ch == "/" and nxt == "*":
            out.append((ch, False, True))
            out.append((nxt, False, True))
            block = True
            i += 2
            continue
        if ch in "\"'":
            quote = ch
            out.append((ch, True, False))
            i += 1
            continue
        out.append((ch, False, False))
        i += 1
    return out


def check_line(text: str, lang: str = "python") -> list:
    """返回问题列表 [{start, length, msg, fix, block}]。block=False 为软提示（放行仅提示）。"""
    issues = []
    if not text.strip():
        return issues
    states = _split_string_states(text, lang)

    # ---- 通用：代码区全角标点（C/Python 都是必错） ----
    for i, (ch, in_str, in_cmt) in enumerate(states):
        if not in_str and not in_cmt and ch in FULLWIDTH:
            issues.append({"start": i, "length": 1, "block": True,
                           "msg": f"全角符号「{ch}」", "fix": f"改为半角「{FULLWIDTH[ch]}」"})

    # ---- 通用：未闭合引号（三引号行除外，避免 docstring 误报） ----
    if '"""' not in text and "'''" not in text:
        quote_pos, quote_ch = -1, None
        for i, (ch, in_str, in_cmt) in enumerate(states):
            if not in_cmt and ch in ("\"", "'") and (quote_ch is None or ch == quote_ch):
                if quote_ch is None:
                    quote_pos, quote_ch = i, ch
                else:
                    quote_pos, quote_ch = -1, None
        if quote_ch is not None:
            issues.append({"start": quote_pos, "length": 1, "block": True,
                           "msg": f"引号「{quote_ch}」没有闭合", "fix": "补上另一半引号"})

    if lang == "c":
        _c_checks(text, states, issues)
    else:
        _py_checks(text, states, issues)

    # ---- 通用：括号不配对（软提示：多为故意换行续写） ----
    stack = []
    for i, (ch, in_str, in_cmt) in enumerate(states):
        if in_str or in_cmt:
            continue
        if ch in "([{":
            stack.append((ch, i))
        elif ch in BRACKET_PAIR:
            if stack and stack[-1][0] == BRACKET_PAIR[ch]:
                stack.pop()
            else:
                issues.append({"start": i, "length": 1, "block": False,
                               "msg": f"多余的「{ch}」", "fix": "检查括号配对"})
    for ch, i in stack:
        issues.append({"start": i, "length": 1, "block": False,
                       "msg": f"「{ch}」尚未闭合", "fix": "若要跨行续写属正常，回车已放行"})
    return issues


def _code_of(states, no_comment=True, code_only=False):
    if code_only:
        return "".join(c for c, s, m in states if not s and not m)
    if no_comment:
        return "".join(c for c, s, m in states if not m)
    return "".join(c for c, s, m in states)


def _py_checks(text, states, issues):
    code = _code_of(states)
    code_only = _code_of(states, code_only=True)

    if STMT_KW.match(text):
        stripped = code.rstrip()
        if stripped and not stripped.endswith((":", "\\", ",")) and not _open_brackets(code_only):
            issues.append({"start": len(text.rstrip()), "length": 1, "block": True,
                           "msg": f"「{STMT_KW.match(text).group(1)}」语句缺少冒号",
                           "fix": "行尾补一个半角冒号 :（注意别用全角：）"})

    if re.match(r"^\s*print\s+[^\s(=]", code):
        issues.append({"start": 0, "length": 5, "block": True,
                       "msg": "print 是函数不是语句（Python3）", "fix": "写成 print(...)"})

    for word, right in (("true", "True"), ("false", "False"), ("null", "None")):
        for m in re.finditer(rf"(?<![\w.]){word}(?![\w])", code_only):
            issues.append({"start": m.start(), "length": len(word), "block": True,
                           "msg": f"Python 里是 {right}", "fix": f"{word} → {right}"})


def _c_checks(text, states, issues):
    code = _code_of(states).rstrip()
    stripped = code.strip()

    if stripped.startswith("#"):  # 预处理行
        if re.match(r"#\s*include\b", stripped):
            if not re.search(r"<[^>]+>|\"[^\"]+\"", stripped):
                issues.append({"start": 0, "length": len(stripped), "block": True,
                               "msg": "#include 后缺少 <文件> 或 \"文件\"",
                               "fix": "如 #include <stdio.h>"})
            else:
                m = re.search(r"<(stdio|stdlib|string|math|ctype)\s*>", stripped)
                if m:
                    issues.append({"start": 0, "length": len(m.group(0)), "block": True,
                                   "msg": f"#include <{m.group(1)}> 少了 .h",
                                   "fix": f"写成 <{m.group(1)}.h>"})
        return
    if not stripped:
        return

    if C_CTRL_KW.match(stripped):
        m = re.search(r"\bfor\s*\(([^()]*)\)", stripped)
        if m and m.group(1).count(";") != 2:
            issues.append({"start": m.start(), "length": 3, "block": True,
                           "msg": "for(...) 头里需要两个分号",
                           "fix": "for (初值; 条件; 增量)"})
        m2 = re.search(r"\b(if|while)\s*\((.*)\)", stripped)
        if m2 and re.search(r"(?<![=!<>+\-*/&|^])=(?!=)", m2.group(2)):
            issues.append({"start": m2.start(), "length": 3, "block": False,
                           "msg": "条件里出现了单独的 =（赋值）",
                           "fix": "判断相等要写 ==（本行已放行，请确认）"})
        return

    if stripped.endswith(C_OK_TAIL):
        return

    # 语句缺分号：函数定义式（类型开头且以 ) 结尾）可能是下一行写 { 的风格，降为软提示
    if C_TYPE_KW.match(stripped) and stripped.endswith(")"):
        issues.append({"start": len(text.rstrip()) - 1, "length": 1, "block": False,
                       "msg": "疑似缺分号；若这是函数定义（下一行是 {）则正常",
                       "fix": "普通语句行尾要加分号 ;"})
    else:
        issues.append({"start": max(0, len(text.rstrip()) - 1), "length": 1, "block": True,
                       "msg": "语句末尾缺少分号 ;", "fix": "行尾补上半角分号 ;"})


def _open_brackets(code: str) -> bool:
    stack = []
    for ch in code:
        if ch in "([{":
            stack.append(ch)
        elif ch in BRACKET_PAIR and stack and stack[-1] == BRACKET_PAIR[ch]:
            stack.pop()
    return bool(stack)


def render_line_html(text: str, issues: list) -> str:
    """把问题位置画成红圈标注的 HTML（用于气泡展示）。"""
    marks = {}
    for it in issues:
        for k in range(it["start"], it["start"] + max(1, it["length"])):
            marks.setdefault(k, it)
    parts = []
    for i, ch in enumerate(text):
        esc = html.escape(ch)
        if i in marks and not marks[i]["block"]:
            parts.append(f'<span style="color:#f5a623;font-weight:700">{esc}</span>')
        elif i in marks:
            parts.append(f'<span style="border:2px solid #e5484d;border-radius:50%;'
                         f'padding:0 1px;color:#e5484d;font-weight:700">{esc}</span>')
        else:
            parts.append(esc)
    line = "".join(parts) or "（空行）"
    items = "".join(
        f'<div>{"⛔" if it["block"] else "⚠️"} {html.escape(it["msg"])} — '
        f'<b>{html.escape(it["fix"])}</b></div>' for it in issues)
    return (f'<div style="font-family:Consolas,monospace;font-size:14px;color:#1a2233;'
            f'background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:6px 8px">'
            f'{line}</div><div style="margin-top:6px;font-size:12px">{items}</div>')
