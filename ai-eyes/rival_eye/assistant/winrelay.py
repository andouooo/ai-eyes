"""Win32 桥接层（纯 ctypes，零额外依赖）：窗口枚举/激活、剪贴板（文本+DIB 图像）、键击注入、全局热键。

用途：把用户输入与屏幕截图"直接接入"用户自己打开的 AI 客户端——
选目标窗口 → 激活 → 截图进剪贴板 Ctrl+V → 文本进剪贴板 Ctrl+V → Enter 发送。
仅支持 Windows；其他平台导入即抛 ImportError，由上层回退到旧 UI。
"""
import ctypes
import ctypes.wintypes as w
import os          # 2026-10-16 补：is_process_elevated / _read_send_log / launch_client 都在用它，
                   # 之前压根没 import → 一律 NameError，还被各处 try/except 静默吞掉
import sys
import time

if sys.platform != "win32":
    raise ImportError("winrelay 仅支持 Windows")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]

CF_UNICODETEXT = 13
CF_DIB = 8
GMEM_MOVEABLE = 0x0002
WM_HOTKEY = 0x0312

# 噪声窗口黑名单（桌面/输入法宿主等）
_NOISE_TITLES = {"Program Manager", "Windows Input Experience", "Windows 输入体验",
                 "MSCTFIME UI", "Default IME"}

kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [w.UINT, ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [w.UINT]

# 前台置前相关：必须声明句柄类型，否则 64 位下参数会被截断（踩过 OverflowError）
user32.SetForegroundWindow.restype = w.BOOL
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.BringWindowToTop.restype = w.BOOL
user32.BringWindowToTop.argtypes = [w.HWND]
user32.SetWindowPos.restype = w.BOOL
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, w.UINT]
user32.AttachThreadInput.restype = w.BOOL
user32.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.c_void_p]
user32.ShowWindow.restype = w.BOOL
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.IsIconic.restype = w.BOOL
user32.IsIconic.argtypes = [w.HWND]
user32.GetForegroundWindow.restype = w.HWND
kernel32.GetCurrentThreadId.restype = w.DWORD
user32.IsZoomed.restype = w.BOOL
user32.IsZoomed.argtypes = [w.HWND]
user32.GetWindowLongW.restype = w.LONG
user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]


def force_foreground(hwnd) -> bool:
    """把窗口强行带到最前，返回是否真的成了前台窗口。

    后台进程调用 SetForegroundWindow 常被系统前台锁拒绝（老版试过一次就直接放弃），
    这里按成功率从高到低三重手段：
    ① 轻敲一次 ALT —— 很多系统上这就解除了前台锁；
    ② AttachThreadInput 借当前前台线程的输入队列，再 BringWindowToTop + SetForegroundWindow；
    ③ 视觉兜底：SetWindowPos 先置 TOPMOST 再取消，保证它浮在最上层（哪怕焦点没抢到，
       用户也看得见"弹出来了"）。
    """
    try:
        if not user32.IsWindow(hwnd):
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        user32.ShowWindow(hwnd, 5)          # SW_SHOW
        if user32.GetForegroundWindow() == hwnd:
            return True
        user32.keybd_event(0x12, 0, 0, 0)   # VK_MENU down
        user32.keybd_event(0x12, 0, 2, 0)   # VK_MENU up
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        tid_me = kernel32.GetCurrentThreadId()
        attached = False
        if tid_fg and tid_fg != tid_me:
            try:
                attached = bool(user32.AttachThreadInput(tid_fg, tid_me, True))
            except Exception:
                attached = False
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                try:
                    user32.AttachThreadInput(tid_fg, tid_me, False)
                except Exception:
                    pass
        if user32.GetForegroundWindow() == hwnd:
            return True
        flags = 0x0001 | 0x0002 | 0x0040     # NOSIZE | NOMOVE | SHOWWINDOW
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)   # HWND_TOPMOST
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)   # HWND_NOTOPMOST
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def list_windows(exclude_titles=()) -> list:
    """枚举可见、有标题的顶层窗口，返回 [(hwnd, title)]，按 Z 序（前台在前）。"""
    results = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x00000080:  # WS_EX_TOOLWINDOW
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or title in _NOISE_TITLES:
            return True
        for ex in exclude_titles:
            if ex and ex in title:
                return True
        results.append((hwnd, title))
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return results


def window_title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


LAST_SEND_UNCONFIRMED = False   # 最近一次发送"送达未得到确认"（只是没找到证据，不代表没发出去）


def activate_window(hwnd, timeout_s: float = 2.0) -> bool:
    """把目标窗口带到前台并**验证确实到位**（GetForegroundWindow 比对，重试至超时）。

    改走 force_foreground（ALT 轻敲 + 借前台线程输入队列 + TOPMOST 兜底）——老版只做
    "ALT keyup + SetForegroundWindow + SetWindowPos(HWND_TOP)"，实测经常拉不动，
    发送链路就会卡在"无法把目标带到前台"（或者更糟：以为到位了，按键全落到别处）。
    """
    if not user32.IsWindow(hwnd):
        return False
    if force_foreground(hwnd):
        return True
    deadline = time.time() + max(0.3, timeout_s)
    while time.time() < deadline:
        time.sleep(0.15)
        if force_foreground(hwnd):
            return True
    return user32.GetForegroundWindow() == hwnd


def _token_elevated(h_process) -> bool:
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(h_process, 0x0008, ctypes.byref(token)) or not token.value:
        return False
    try:
        elev = w.DWORD()
        ret_len = w.DWORD()
        ok = advapi32.GetTokenInformation(token, 20, ctypes.byref(elev), 4, ctypes.byref(ret_len))
        return bool(ok and elev.value)  # TokenElevation
    finally:
        kernel32.CloseHandle(token)


def is_process_elevated(pid: int) -> bool:
    """目标进程是否以管理员运行。若目标高权限而本程序不是，合成按键会被 UIPI 静默丢弃。"""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        return _token_elevated(h)
    finally:
        kernel32.CloseHandle(h)


def self_elevated() -> bool:
    import os

    return is_process_elevated(os.getpid())


# ---------- 剪贴板 ----------

def _open_clipboard(retries: int = 20) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(0):
            return True
        time.sleep(0.05)
    return False


def set_clipboard_text(text: str) -> bool:
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


def set_clipboard_image(img) -> bool:
    """PIL RGB 图 → DIB 进剪贴板（去掉 14 字节 BITMAPFILEHEADER 的 BMP）。"""
    import io

    buf = io.BytesIO()
    img.save(buf, format="BMP")
    dib = buf.getvalue()[14:]
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, dib, len(dib))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_DIB, handle))
    finally:
        user32.CloseClipboard()


def clipboard_text() -> str:
    """读回剪贴板文本（仅供自检）。"""
    if not _open_clipboard():
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


# ---------- 键击 ----------

def _key(vk: int, up: bool = False) -> None:
    user32.keybd_event(vk, 0, 2 if up else 0, 0)


def send_ctrl_v() -> None:
    _key(0x11)
    _key(0x56)
    _key(0x56, True)
    _key(0x11, True)


def send_ctrl_c() -> None:
    _key(0x11)
    _key(0x43)
    _key(0x43, True)
    _key(0x11, True)


def send_ctrl_a() -> None:
    _key(0x11)
    _key(0x41)
    _key(0x41, True)
    _key(0x11, True)


def _click(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)
    time.sleep(0.03)
    user32.mouse_event(2, 0, 0, 0, 0)   # 左键按下
    time.sleep(0.03)
    user32.mouse_event(4, 0, 0, 0, 0)   # 左键抬起


def _uia_each_edit(hwnd, visit):
    """遍历窗口内的可编辑控件（Edit/Document），逐个回调 visit(control)；任何异常静默。"""
    try:
        import uiautomation as ua

        root = ua.ControlFromHandle(hwnd)
        if root is None:
            return
        stack = [root]
        n = 0
        while stack and n < 8000:
            c = stack.pop()
            n += 1
            try:
                if c.ControlType in (ua.ControlType.EditControl, ua.ControlType.DocumentControl):
                    visit(c)
            except Exception:
                pass
            try:
                kids = c.GetChildren()
                if kids:
                    stack.extend(reversed(kids))
            except Exception:
                pass
    except Exception:
        pass


def _uia_chat_input_rect(hwnd):
    """UIA 定位聊天输入框矩形（屏幕坐标）：窗口下半部、最靠底的可编辑控件。"""
    try:
        import uiautomation as ua

        rect = window_bounds(hwnd)
        win_h = rect.bottom - rect.top
        found = []

        def visit(c):
            r = c.BoundingRectangle
            if not r or (r.right - r.left) < 100 or not (0 < (r.bottom - r.top) < 400):
                return
            cy = (r.top + r.bottom) // 2
            if cy > rect.top + win_h * 0.5:  # 只认下半屏的编辑框（聊天输入都在底部）
                found.append((r.bottom, (r.left, r.top, r.right, r.bottom)))

        _uia_each_edit(hwnd, visit)
        if not found:
            return None
        found.sort()
        return found[-1][1]
    except Exception:
        return None


def _uia_chat_input_point(hwnd):
    """输入框中心坐标（供点击聚焦）。"""
    rect = _uia_chat_input_rect(hwnd)
    if rect is None:
        return None
    return (rect[0] + rect[2]) // 2, rect[3] - 25  # 中心偏上，避免点到边框


def _band_of(img):
    band_h = max(160, int(img.height * 0.22))
    return img.crop((0, max(0, img.height - band_h), img.width, img.height))


def _attachment_chip_visible(hwnd, before_img) -> bool:
    """输入框**上方那一条**（附件缩略图出现的位置）有没有出现新东西。

    客户端把粘贴的图片挂到输入框上方变成缩略图；只比对输入框本身会漏掉它。
    没坐标就退回整窗比对；任何异常都返回 False（宁可多重贴一次，也不要静默丢图）。
    """
    try:
        from PIL import ImageChops

        cur = capture_window(hwnd)
        if cur is None or before_img is None:
            return False
        rect = _uia_chat_input_rect(hwnd)
        if rect:
            wb = window_bounds(hwnd)
            x1 = max(0, rect[0] - wb.left - 20)
            x2 = min(before_img.width, rect[2] - wb.left + 20)
            y2 = max(0, rect[1] - wb.top - 6)          # 输入框上沿
            y1 = max(0, y2 - 120)                       # 往上看 120px
            if x2 - x1 < 30 or y2 - y1 < 20:
                return False
            z1 = before_img.crop((x1, y1, x2, y2))
            z2 = cur.crop((x1, y1, x2, y2))
        else:
            z1, z2 = _band_of(before_img), _band_of(cur)
        bbox = ImageChops.difference(z1, z2).getbbox()
        return bbox is not None and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) >= 300
    except Exception:
        return False


def _input_zone_changed(hwnd, before_img) -> bool:
    """像素兜底验证（窄条）：只对比 UIA 定位的输入框附近窄条（下扩 80px 容纳附件条
    下移、上收避开正在流式输出的对话区）。exe 里 WinRT OCR 失效时用，截图在冻结环境稳定。"""
    try:
        from PIL import ImageChops

        cur = capture_window(hwnd)
        if cur is None or before_img is None:
            return False
        rect = _uia_chat_input_rect(hwnd)
        if rect:
            wb = window_bounds(hwnd)
            x1 = max(0, rect[0] - wb.left - 20)
            y1 = max(0, rect[1] - wb.top - 10)
            x2 = min(before_img.width, rect[2] - wb.left + 20)
            y2 = min(before_img.height, rect[3] - wb.top + 80)
            if x2 - x1 < 30 or y2 - y1 < 20:
                z1, z2 = _band_of(before_img), _band_of(cur)
            else:
                z1 = before_img.crop((x1, y1, x2, y2))
                z2 = cur.crop((x1, y1, x2, y2))
        else:
            z1, z2 = _band_of(before_img), _band_of(cur)
        bbox = ImageChops.difference(z1, z2).getbbox()
        return bbox is not None and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) >= 250
    except Exception:
        return False


def _any_big_change(hwnd, before_img, min_area: int = 2000) -> bool:
    '''整窗宽限判定：贴前快照 vs 现在有任何实质像素变化（粘贴痕迹）。'''
    try:
        from PIL import ImageChops
        cur = capture_window(hwnd)
        if cur is None or before_img is None:
            return False
        bbox = ImageChops.difference(before_img, cur).getbbox()
        return bbox is not None and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) >= min_area
    except Exception:
        return False


def _paste_landed(hwnd, snippet: str, pre_shot=None, quick: bool = False) -> bool:
    """验证粘贴是否真的落进输入区，三级：
    ①UIA 读可编辑控件的值；②视觉 OCR 底部整带（源码环境可用；exe 里 WinRT OCR 失效返回空）；
    ③窄条像素对比：贴前快照 vs 现在，输入框附近有实质变化即视为送达。
    quick=True（极速中继用）跳过 OCR 那一级，只走 UIA + 像素——OCR 太慢，但**不能整级跳过**：
    13:56 实测极速模式信任粘贴 + 回车，结果消息根本没发出去（输入框还被清空了）。
    """
    if not snippet:
        return False
    if _uia_input_contains(hwnd, snippet):
        return True
    if not quick:
        try:
            img = capture_window(hwnd)
            if img is None:
                return False
            from PIL import Image

            band = _band_of(img)
            band = band.resize((band.width * 2, band.height * 2), Image.Resampling.LANCZOS)
            from .reply import ocr_image

            text = ocr_image(band) or ""
            if "".join(snippet.split())[-12:] in "".join(text.split()):
                return True
        except Exception:
            pass
    return _input_zone_changed(hwnd, pre_shot)


def _delivered_to_transcript(hwnd, snippet: str, timeout: float = 2.5) -> bool:
    """回车后确认这条消息**真的进了对话区**（出现在输入框上方的文本里）。

    对"输入框值读不出来"的 Electron 客户端（DSH/ZCode 这种），这是唯一可信的送达证据：
    消息气泡是输入框上方的新文本节点，而输入框里残留的草稿在下方，用坐标就能分开。
    返回 False 只代表"没找到证据"，不代表一定没发出去——调用方按"警告 + 内容留剪贴板"处理。
    """
    key = "".join((snippet or "").split())
    if not key:
        return True
    tail = key[-16:]
    try:
        import uiautomation as ua
    except ImportError:
        return True
    try:
        root = ua.ControlFromHandle(hwnd)
        in_rect = _uia_chat_input_rect(hwnd)
    except Exception:
        return True
    if root is None:
        return True
    t0 = time.time()
    while time.time() - t0 < max(0.4, timeout):
        stack, n = [root], 0
        while stack and n < 8000:
            c = stack.pop()
            n += 1
            try:
                nm = (c.Name or "").strip()
                if nm and tail in "".join(nm.split()):
                    if in_rect is None:
                        return True
                    try:
                        r = c.BoundingRectangle
                        if not r or r.bottom <= in_rect[1]:
                            return True      # 在输入框上方 = 对话区
                    except Exception:
                        return True
            except Exception:
                pass
            try:
                kids = c.GetChildren()
                if kids:
                    stack.extend(reversed(kids))
            except Exception:
                pass
        time.sleep(0.3)
    return False


def _uia_input_contains(hwnd, snippet: str) -> bool:
    """验证文本是否真的落进了输入框（读可编辑控件的 Value）。"""
    if not snippet:
        return False
    hit = [False]

    def visit(c):
        if hit[0]:
            return
        try:
            val = c.GetValue() or ""
        except Exception:
            val = ""
        if snippet in val:
            hit[0] = True

    _uia_each_edit(hwnd, visit)
    return hit[0]


def send_home() -> None:
    _key(0x24)
    _key(0x24, True)


def send_end() -> None:
    _key(0x23)
    _key(0x23, True)


def send_shift_end() -> None:
    _key(0x10)      # Shift 按下
    _key(0x23)      # End
    _key(0x23, True)
    _key(0x10, True)


def send_enter_key() -> None:
    _key(0x0D)
    _key(0x0D, True)


def _send_log(msg: str) -> None:
    """发送链路逐步留痕（追加到可执行文件/项目目录旁，便于排查"没发出去"）。"""
    import datetime
    import os
    import sys

    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        path = os.path.join(base, "RivalEye-发送日志.txt")
        text = open(path, encoding="utf-8", errors="replace").read() if os.path.exists(path) else ""
        lines = (text.splitlines() + [f"[{datetime.datetime.now():%H:%M:%S}] {msg}"])[-60:]
        open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    except Exception:
        pass


def _click_force_send(hwnd) -> bool:
    """部分客户端（如 ZCode 在 AI 运行中）回车只把消息放进"待发送"队列而非发出。
    找输入区附近的「立即/立即发送」按钮点一下强制送达；没有则什么都不做。"""
    try:
        import uiautomation as ua

        zone = _uia_chat_input_rect(hwnd)
        found = []
        wb = None   # 2026-10-16 补：下面 zone is None 那个分支要用它。
                    # 原代码直接用 wb，那是隔壁 background_send() 的局部变量，跨函数拿不到 →
                    # NameError → 被函数末尾的 except Exception: pass 吞掉 →
                    # 这个「点立即发送」的补救逻辑实际上一次都没跑成过。
        stack = [ua.ControlFromHandle(hwnd)]
        n = 0
        t0 = time.time()
        while stack and n < 3000 and time.time() - t0 < 1.5:  # 限时，别拖慢发送
            c = stack.pop()
            n += 1
            try:
                if c.ControlType == ua.ControlType.ButtonControl:
                    nm = (c.Name or "").strip()
                    if nm in ('立即', '立即发送') or nm in (chr(31435)+chr(21363)):
                        r = c.BoundingRectangle
                        if r and zone is not None and abs(((r.top + r.bottom) // 2) - zone[3]) < 350:
                            found.append(((r.left + r.right) // 2, (r.top + r.bottom) // 2))
                        elif r and zone is None:
                            if wb is None:              # 用到才取，省一次 UIA 调用
                                wb = window_bounds(hwnd)
                            if wb and r.top > wb.top + (wb.bottom - wb.top) * 0.6:
                                found.append(((r.left + r.right) // 2, (r.top + r.bottom) // 2))
            except Exception:
                pass
            try:
                kids = c.GetChildren()
                if kids:
                    stack.extend(reversed(kids))
            except Exception:
                pass
        if found:
            _click(*found[-1])
            return True
    except Exception:
        pass
    return False


def background_send(hwnd, text: str):
    '''UIA 后台发送：ValuePattern 直接写目标输入框 + Invoke 触发发送按钮，
    全程不激活窗口、不移动焦点（游戏零感知）。返回 (ok, err)。'''
    try:
        import uiautomation as ua
    except ImportError:
        return False, '无 UIA 支持'
    try:
        root = ua.ControlFromHandle(hwnd)
        if root is None:
            return False, '目标窗口不可访问'
        wb = window_bounds(hwnd)

        def collect(ctl_type, pred=None):
            out = []
            stack = [root]
            n = 0
            while stack and n < 8000:
                c = stack.pop()
                n += 1
                try:
                    if c.ControlType == ctl_type and (pred is None or pred(c)):
                        out.append(c)
                except Exception:
                    pass
                try:
                    kids = c.GetChildren()
                    if kids:
                        stack.extend(reversed(kids))
                except Exception:
                    pass
            return out

        # 1) 输入框：窗口下半部最靠底的可编辑控件
        def is_input(c):
            r = c.BoundingRectangle
            if not r or (r.right - r.left) < 100 or not (0 < (r.bottom - r.top) < 400):
                return False
            return (r.top + r.bottom) / 2 > wb.top + (wb.bottom - wb.top) * 0.5
        edits = collect(ua.ControlType.EditControl, is_input) + collect(ua.ControlType.DocumentControl, is_input)
        if not edits:
            return False, '未找到输入框'
        edit = max(edits, key=lambda c: c.BoundingRectangle.bottom)

        # 2) 后台写入文本（不抢焦点；SetValue 是替换语义，无重复风险）
        try:
            edit.GetValuePattern().SetValue(text)
        except Exception:
            return False, '输入框不支持后台写入'
        time.sleep(0.4)
        r = edit.BoundingRectangle
        in_cy = (r.top + r.bottom) / 2

        # 3) 发送按钮：与输入框同行、靠右的按钮
        def is_send_btn(c):
            br = c.BoundingRectangle
            if not br or br.left < r.right - 320:
                return False
            return abs((br.top + br.bottom) / 2 - in_cy) < 100
        buttons = collect(ua.ControlType.ButtonControl, is_send_btn)
        if not buttons:
            return False, '未找到发送按钮'
        buttons.sort(key=lambda c: c.BoundingRectangle.left)
        named = [c for c in buttons if any(k in (c.Name or '') for k in ('发送', 'send', 'Send', '立即'))]
        ok = False
        for c in (named or [buttons[-1]]):
            try:
                c.GetInvokePattern().Invoke()
                ok = True
                break
            except Exception:
                try:
                    c.GetLegacyIAccessiblePattern().DoDefaultAction()
                    ok = True
                    break
                except Exception:
                    continue
        if not ok:
            return False, '发送按钮无法程序化点击'
        time.sleep(0.5)
        # 4) 验证：输入框清空 = 已发送
        try:
            if not (edit.GetValue() or '').strip():
                return True, ''
        except Exception:
            pass
        return True, '已触发发送（未确认清空）'
    except Exception as e:
        return False, str(e)[:60]


def relay_to_window(hwnd, text: str, image=None, send_enter: bool = True, restore_to=None, fast=False):
    """把 [截图(可选)] + 文本 粘贴进目标 AI 客户端窗口并回车。返回 (ok, err)。

    激活与粘贴均做真实校验：前台不到位 / 目标高权限会如实报错，
    失败时内容仍会留在剪贴板，可手动到目标窗口 Ctrl+V。逐步结果写入 RivalEye-发送日志.txt。
    """
    if not text and image is None:
        return False, "没有可发送的内容"
    if not user32.IsWindow(hwnd):
        _send_log(f"失败：目标窗口已关闭 hwnd={hwnd}")
        return False, "目标窗口已关闭，请刷新列表重选"
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value and is_process_elevated(pid.value) and not self_elevated():
        _send_log(f"失败：目标进程(pid={pid.value})是管理员，本程序不是，按键被 UIPI 拦截")
        return False, "目标窗口以管理员运行，系统会拦截本程序的按键；请右键 RivalEye.exe 选「以管理员身份运行」后重试"
    prev_fg = user32.GetForegroundWindow()  # 发送前用户所在窗口，结束后归还焦点
    if not activate_window(hwnd):
        if image is not None:
            set_clipboard_image(image)
        elif text:
            set_clipboard_text(text)
        _send_log(f"失败：无法把目标带到前台（已把内容留在剪贴板）「{window_title(hwnd)[:30]}」")
        return False, "无法把目标窗口带到前台（内容已复制到剪贴板）：请手动点一下目标窗口按 Ctrl+V"
    _send_log(f"已激活目标「{window_title(hwnd)[:30]}」" + ("（极速）" if fast else ""))
    try:
        time.sleep(0.15 if fast else 0.3)
        if image is not None:
            if not set_clipboard_image(image):
                _send_log("失败：剪贴板写入截图失败")
                return False, "剪贴板写入截图失败"
            pre_img = capture_window(hwnd)
            send_ctrl_v()
            time.sleep(0.35 if fast else 0.6)  # 等客户端把图片挂上输入框
            # 关键：Electron 系客户端处理粘贴是异步的。0.35 秒后就把剪贴板换成文字，
            # 客户端读到的可能已经是**文字**，图片直接丢——实测"截图没出"就是这么来的。
            # 所以先确认"附件缩略图真的出现了"，没出现就再贴一次并多等，确认后才覆盖剪贴板。
            ok_chip = _attachment_chip_visible(hwnd, pre_img)
            for _i in range(3):
                if ok_chip:
                    break
                time.sleep(0.45)
                ok_chip = _attachment_chip_visible(hwnd, pre_img)
            if not ok_chip:
                try:
                    set_clipboard_image(image)
                    send_ctrl_v()
                    time.sleep(0.7)
                    ok_chip = _attachment_chip_visible(hwnd, pre_img)
                except Exception:
                    pass
            _send_log("已注入：截图 Ctrl+V" + ("" if ok_chip else "（未检出附件缩略图，可能没挂上）"))
        if text:
            key = text[-20:]
            if not set_clipboard_text(text):
                _send_log("失败：剪贴板写入文本失败")
                return False, "剪贴板写入文本失败"
            pre_shot = capture_window(hwnd)  # 贴前快照（像素兜底验证用）
            send_ctrl_v()
            time.sleep(0.3 if fast else 0.4)
            # 极速模式也不能跳过验证：13:56 实测"信任粘贴 + 直接回车"会让消息彻底丢失
            landed = _paste_landed(hwnd, key, pre_shot, quick=fast)
            _send_log(f"首次粘贴{'已验证落入输入区' if landed else '未检出，尝试点击输入框重试'}")
            if not landed:
                # 粘贴没落进输入区（窗口激活后焦点在别处）：UIA 定位输入框并点击聚焦，重贴
                pt = _uia_chat_input_point(hwnd)
                if pt is None:
                    r = window_bounds(hwnd)
                    pt = ((r.left + r.right) // 2, r.bottom - max(40, (r.bottom - r.top) // 12))
                _click(*pt)
                time.sleep(0.35)
                pre_shot2 = capture_window(hwnd)
                send_ctrl_v()
                time.sleep(0.4)
                landed = _paste_landed(hwnd, key, pre_shot2, quick=fast)
                _send_log(f"重试粘贴{'已验证落入输入区' if landed else '仍未检出'}")
                if not landed and _any_big_change(hwnd, pre_shot):
                    landed = True
                    _send_log('宽限判定：整窗有粘贴痕迹，视为已送达')
                if not landed:
                    set_clipboard_text(text)  # 内容留在剪贴板供手动粘贴
                    return False, "无法把内容放进目标输入框（内容已复制）：请手动点一下目标输入框按 Ctrl+V"
        if send_enter:
            # 回车前确认目标仍在前台（用户随时可能点别的窗口抢走焦点），被抢则重新激活
            if user32.GetForegroundWindow() != hwnd:
                if not activate_window(hwnd):
                    set_clipboard_text(text)
                    _send_log("失败：回车前目标失焦且无法重新激活（内容已留剪贴板）")
                    return False, "回车前目标窗口失焦（内容已复制到剪贴板）：请再点一次发送重试"
                time.sleep(0.2)
            send_enter_key()
            time.sleep(0.5 if fast else 0.35)
            clicked = False
            # 排队条（ZCode 等）要 200-500ms 才渲染出来，所以回车后还得找一次「立即」。
            # 只找 2 轮：每次 _click_force_send 在超大无障碍树（如 Electron 客户端）里
            # 要花满 1.5 秒预算，4 轮会让"发送后"白等 6-8 秒——而绝大多数客户端根本没有
            # 这个按钮，白等的代价大于收益。
            for _attempt in range(2):
                if _click_force_send(hwnd):
                    clicked = True
                    break
                time.sleep(0.4)
            if clicked:
                _send_log("已点击「立即」按钮强制送达（目标处于排队模式）")
            else:
                _send_log("已注入：回车（未见排队模式）")
            # 回车后做一次**正向送达确认**：消息出现在输入框上方的对话区吗？
            # 没有就补发一次回车；仍没有则如实报"未确认送达"（内容留剪贴板）——
            # 静默丢失比误报失败更糟（13:56 快捷键那条就是这么没了，日志却全绿）。
            if text and not _delivered_to_transcript(hwnd, text, timeout=2.0 if fast else 2.5):
                send_enter_key()
                time.sleep(0.4)
                if not _delivered_to_transcript(hwnd, text, timeout=2.0):
                    # 只是"没找到证据"，不等于没发出去（实测 13:04 那次消息其实送达了，
                    # 这里误判成失败 → 上层不启动回复监听 → 整轮闭环断掉，比误报更糟）。
                    # 所以如实记日志 + 打标记，但仍按成功返回，让回复监听照跑。
                    global LAST_SEND_UNCONFIRMED
                    LAST_SEND_UNCONFIRMED = True
                    _send_log("警告：回车后未在对话区看到这条消息（送达未确认，仍按已发送处理）")
                else:
                    _send_log("补发一次回车后已确认送达")
        _send_log("发送链路完成（回车前已确认前台）")
        return True, ""
    finally:
        try:
            back = restore_to if (restore_to and user32.IsWindow(restore_to)) else prev_fg
            if back and back != hwnd and user32.IsWindow(back):
                user32.keybd_event(0x12, 0, 2, 0)
                user32.SetForegroundWindow(back)  # 焦点还给指定窗口（如游戏）或原窗口
                _send_log("已把焦点还原给原窗口")
        except Exception:
            pass


# ---------- 全局热键（Ctrl+Alt+S 截图，需在 GUI 主线程注册） ----------

HOTKEY_ID = 0xB51
HOTKEY_FEEDBACK_ID = 0xB52

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008


def register_hotkey(hotkey_id: int, mods: int, vk: int) -> bool:
    return bool(user32.RegisterHotKey(None, hotkey_id, mods, vk))


def register_capture_hotkey() -> bool:
    return register_hotkey(HOTKEY_ID, MOD_CONTROL | MOD_ALT, 0x53)


def unregister_capture_hotkey() -> None:
    user32.UnregisterHotKey(None, HOTKEY_ID)

def unregister_feedback_hotkey() -> None:
    user32.UnregisterHotKey(None, HOTKEY_FEEDBACK_ID)


# ---------- 单实例（互斥 + 唤醒广播） ----------

ERROR_ALREADY_EXISTS = 183
HWND_BROADCAST = 0xFFFF
_mutex = None
_SHOW_MSG = None


def acquire_single_instance() -> bool:
    """命名互斥；已有实例在跑时返回 False（此时应 broadcast_show_console 唤醒它后退出）。"""
    global _mutex
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
    _mutex = kernel32.CreateMutexW(None, False, "RivalEye_SingleInstance")
    return kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def show_console_message() -> int:
    """自定义窗口消息 id：已有实例收到后弹出控制台（值 >= 0xC000，不会与系统消息冲突）。"""
    global _SHOW_MSG
    if _SHOW_MSG is None:
        _SHOW_MSG = user32.RegisterWindowMessageW("RivalEyeShowConsole")
    return _SHOW_MSG


def broadcast_show_console() -> None:
    user32.PostMessageW(HWND_BROADCAST, show_console_message(), 0, 0)


# ---------- 窗口捕获（OBS 式锁定窗口，支持被遮挡） ----------

gdi32 = ctypes.windll.gdi32
PW_RENDERFULLCONTENT = 2

# GDI 句柄在 64 位进程下可能超出 c_int 范围，必须显式声明，否则偶发 OverflowError
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.UINT, w.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, w.UINT]
user32.GetWindowDC.restype = ctypes.c_void_p
user32.GetWindowDC.argtypes = [w.HWND]
user32.ReleaseDC.argtypes = [w.HWND, ctypes.c_void_p]
user32.PrintWindow.argtypes = [w.HWND, ctypes.c_void_p, w.UINT]


class _BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD),
    ]


def window_bounds(hwnd):
    """窗口可见边框（优先 DWM 扩展边框，去掉 Win10 不可见阴影边距）。"""
    rect = w.RECT()
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))  # DWMWA_EXTENDED_FRAME_BOUNDS
        if rect.right > rect.left and rect.bottom > rect.top:
            return rect
    except Exception:
        pass
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT),
                ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


def make_borderless(hwnd) -> bool:
    """把窗口变成"无边框全屏"（去掉标题栏、铺满它所在的那块屏幕）。

    为什么需要它：独占全屏的游戏一被抢焦点就会黑屏/掉回桌面，任何普通窗口都没法在不抢焦点的
    情况下接受中文输入（没有焦点的窗口用不了输入法）。所以"游戏里弹窗还能打字"的唯一现实做法
    是让游戏跑**无边框窗口化**——先把游戏显示模式设成"窗口"，再用这个函数去掉边框。
    """
    try:
        if not user32.IsWindow(hwnd):
            return False
        style = user32.GetWindowLongW(hwnd, -16)          # GWL_STYLE
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        mon = user32.MonitorFromWindow(hwnd, 2)           # MONITOR_DEFAULTTONEAREST
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return False
        r = mi.rcMonitor
        new_style = (style & ~0x00C00000) | 0x80000000    # 去掉 WS_CAPTION，加上 WS_POPUP
        user32.SetWindowLongW(hwnd, -16, new_style)
        user32.SetWindowPos(hwnd, -1, r.left, r.top, r.right - r.left, r.bottom - r.top,
                            0x0040)                        # SWP_SHOWWINDOW
        return True
    except Exception:
        return False


def capture_window(hwnd, max_width: int | None = None):
    """PrintWindow 捕获窗口内容（被遮挡也能截到，Chromium/Electron 有效）。

    最小化时返回 None。返回 PIL RGB 图，超过 max_width 时降采样。
    """
    from PIL import Image

    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return None
    rect = window_bounds(hwnd)
    wdt, hgt = rect.right - rect.left, rect.bottom - rect.top
    if wdt <= 0 or hgt <= 0:
        return None

    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, wdt, hgt)
    try:
        gdi32.SelectObject(mem_dc, bmp)
        ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
        if not ok:
            ok = user32.PrintWindow(hwnd, mem_dc, 0)
        if not ok:
            return None
        header = _BMIH()
        header.biSize = ctypes.sizeof(header)
        header.biWidth, header.biHeight = wdt, -hgt  # 负高 = top-down
        header.biPlanes, header.biBitCount, header.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(wdt * hgt * 4)
        if not gdi32.GetDIBits(mem_dc, bmp, 0, hgt, buf, ctypes.byref(header), 0):
            return None
        img = Image.frombytes("RGBA", (wdt, hgt), buf.raw)  # 内存布局实为 BGRA
        b, g, r, _a = img.split()
        img = Image.merge("RGB", (r, g, b))
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
    if max_width and img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)),
                         Image.Resampling.BILINEAR)
    return img

# ---------- AI 客户端启动 ----------

def find_client_exe() -> str:
    import os
    '''探测 AI 客户端可执行文件（当前识别 ZCode 桌面版）。返回路径或空串。'''
    import glob
    pats = [
        os.path.expandvars(r'%LOCALAPPDATA%/Programs/ZCode/ZCode.exe'),
        'E:/桌面/zode/ZCode/ZCode.exe',
        'C:/Program Files/ZCode/ZCode.exe',
    ]
    for p in pats:
        if os.path.exists(p):
            return p
    for pat in ('E:/*/zode/ZCode/ZCode.exe', 'E:/桌面/*/ZCode/ZCode.exe',
                'D:/*/ZCode/ZCode.exe'):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return ''

def launch_client(exe_path: str) -> bool:
    '''独立进程启动 AI 客户端（DETACHED，不随本程序退出）。'''
    import subprocess
    try:
        subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=0x00000004)
        return True
    except Exception:
        return False
