"""Qt(PySide6) 助手 UI：悬浮球入口 + 白底控制台。

控制台布局（按 2026-10-14 需求）：
  顶部：选择"已打开的 AI 客户端"窗口 → 之后输入将直接注入该客户端；
  右侧：截取画面预览（按钮 / Ctrl+Alt+S 全局热键 / 可选自动刷新）；
  左侧：聊天式输入框（Ctrl+Enter 发送）。
发送 = 激活目标窗口 → [截图→剪贴板→Ctrl+V] → 文本→剪贴板→Ctrl+V → Enter。
旧的内嵌 VLM 聊天面板由 Tk 后备（overlay.py）保留。
"""
import os
import re
import sys
import threading
import time

import ctypes.wintypes as w

from PySide6.QtCore import QEvent, QSize, QAbstractNativeEventFilter, QObject, QRect, QSettings, Qt, QTimer, Signal  # QTimer 2026-10-16 补：_on_task_done 缺它 → NameError 被 exe 无控制台吞掉 → 回复到达永不弹前台
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import guard, reply, winrelay
from .screenshot import grab_box_image, grab_monitor_image

DRAG_THRESHOLD = 6
SEND_IMAGE_MAX_W = 2560  # 发送给 AI 客户端的截图最大宽度；单显示器 ≤2560 宽时为原生直出
PREVIEW_MAX_W = 1920     # 实时预览的最大宽度


class DragMixin:
    """通用拖拽：按住移动窗口；位移小于阈值视为点击并发出 clicked。"""

    def _init_drag(self):
        self._offset = None
        self._start = None
        self._moved = False

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._start = e.globalPosition().toPoint()
            self._moved = False

    def mouseMoveEvent(self, e):
        if self._offset is None:
            return
        gp = e.globalPosition().toPoint()
        if (gp - self._start).manhattanLength() > DRAG_THRESHOLD:
            self._moved = True
        self.move(gp - self._offset)

    def mouseReleaseEvent(self, e):
        if self._offset is not None and not self._moved and hasattr(self, "clicked"):
            self.clicked.emit()
        self._offset = None


class BallWidget(QWidget, DragMixin):
    clicked = Signal()

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self._init_drag()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(72, 72)

    def paintEvent(self, _e):
        '''卡通小雷达：不透明圆形身体 + 眼睛 + 微笑 + 腮红 + 天线。'''
        from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QRadialGradient
        from PySide6.QtCore import QRectF, QPointF

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(4, 4, 64, 64)
        grad = QRadialGradient(QPointF(28, 26), 46)
        grad.setColorAt(0, QColor('#fffdf5'))
        grad.setColorAt(1, QColor('#ffd98a'))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor('#e8613c'), 3))
        p.drawEllipse(body)  # 不透明圆底
        # 天线
        p.setPen(QPen(QColor('#e8613c'), 3))
        p.drawLine(36, 6, 36, 1)
        p.setBrush(QColor('#e8613c'))
        p.drawEllipse(QRectF(31, -2, 10, 10))
        # 眼睛
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#2b2b2b'))
        p.drawEllipse(QRectF(22, 26, 9, 11))
        p.drawEllipse(QRectF(41, 26, 9, 11))
        p.setBrush(QColor('#ffffff'))
        p.drawEllipse(QRectF(24.5, 28, 3.4, 4))
        p.drawEllipse(QRectF(43.5, 28, 3.4, 4))
        # 微笑
        p.setPen(QPen(QColor('#2b2b2b'), 2.6))
        p.drawArc(QRectF(26, 40, 20, 14), 20 * 16, 140 * 16)
        # 腮红
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 150, 150, 170))
        p.drawEllipse(QRectF(15, 38, 9, 6))
        p.drawEllipse(QRectF(48, 38, 9, 6))

    def contextMenuEvent(self, e):
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.addAction("打开控制台", self.clicked.emit)
        menu.addSeparator()
        menu.addAction("退出竞眼雷达", QApplication.instance().quit)
        menu.exec(e.globalPos())


class RegionPicker(QWidget):
    """屏幕选区遮罩：拖拽画矩形，松开把选区（换算成 mss 物理坐标）经 picked 发出；Esc 取消。

    坐标映射：Qt 逻辑坐标 → 物理像素，按 所在显示器的物理/逻辑尺寸比例换算，
    不受 Windows DPI 缩放影响。
    """

    picked = Signal(dict)
    cancelled = Signal()

    MIN_LOGICAL = 8  # 小于该逻辑像素的框视为误触

    def __init__(self, qscr, mon_box: dict):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._scr = qscr
        self._mon = mon_box
        geo = qscr.geometry()
        self._sx = mon_box["width"] / max(1, geo.width())
        self._sy = mon_box["height"] / max(1, geo.height())
        self.setGeometry(geo)
        self._start = None
        self._rect = None

    def show_picker(self):
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    # ---------- 交互 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = e.globalPosition().toPoint()
            self._rect = QRect(self._start, self._start)
            self.update()

    def mouseMoveEvent(self, e):
        if self._start is not None:
            self._rect = QRect(self._start, e.globalPosition().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._rect is not None:
            if self._rect.width() >= self.MIN_LOGICAL and self._rect.height() >= self.MIN_LOGICAL:
                self.picked.emit(self._to_phys(self._rect))
            else:
                self.cancelled.emit()
            self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()

    # ---------- 坐标换算与绘制 ----------
    def _to_phys(self, r: QRect) -> dict:
        g = self._scr.geometry()
        left = int(round(self._mon["left"] + (r.x() - g.x()) * self._sx))
        top = int(round(self._mon["top"] + (r.y() - g.y()) * self._sy))
        w = max(1, int(round(r.width() * self._sx)))
        h = max(1, int(round(r.height() * self._sy)))
        w = min(w, self._mon["left"] + self._mon["width"] - left)
        h = min(h, self._mon["top"] + self._mon["height"] - top)
        return {"left": left, "top": top, "width": max(1, w), "height": max(1, h)}

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self._rect and self._rect.width() > 1:
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(self._rect, Qt.transparent)  # 选区内"挖洞"露出真实画面
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.setPen(QPen(QColor("#5b8cff"), 2))
            p.drawRect(self._rect)
            tag = f"{int(self._rect.width() * self._sx)} × {int(self._rect.height() * self._sy)}"
            p.setPen(QColor("#ffffff"))
            p.drawText(self._rect.x() + 6, max(14, self._rect.y() - 8), f"{tag}  (松开确认 / Esc 取消)")
        else:
            p.setPen(QColor("#ffffff"))
            p.drawText(self.rect(), 0x0004 | 0x0080, "拖拽选取要截取的范围 · Esc 取消")


class GuardTip(QWidget):
    """代码守卫气泡：无焦点置顶小窗，圈出错字 + 改法；「允许换行」或 3 秒后再按回车强制放行。"""

    allow_requested = Signal()

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        self.setStyleSheet("background:#ffffff; border:2px solid #e5484d; border-radius:10px;")
        self.body = QLabel()
        self.body.setTextFormat(Qt.RichText)
        self.body.setWordWrap(True)
        lay.addWidget(self.body)
        row = QHBoxLayout()
        hint = QLabel("⛔ 已拦截本次回车 · 3 秒内再按一次回车强制换行")
        hint.setStyleSheet("color:#6b7280; font-size:11px; background:transparent; border:none;")
        row.addWidget(hint, 1)
        b = QPushButton("允许换行")
        b.setStyleSheet(_BTN_LIGHT)
        b.clicked.connect(self._allow)
        row.addWidget(b)
        lay.addLayout(row)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def _allow(self):
        self.allow_requested.emit()
        self.hide()

    def popup(self, html_text: str, x: int, y: int):
        self.body.setText(html_text)
        self.adjustSize()
        self.move(x, y)
        self.show()
        self.raise_()
        self._timer.start(6000)


class ChatInput(QTextEdit):
    """聊天输入框：Enter 直接发送，Shift+Enter 换行（聊天界面惯例）。"""

    send_requested = Signal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not e.modifiers() & (
                Qt.ShiftModifier | Qt.ControlModifier | Qt.AltModifier):
            self.send_requested.emit()
            return
        super().keyPressEvent(e)


class KeyGrabDialog(QDialog):
    '''热键录制小窗：按下组合键记录，Esc 取消。'''

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setFixedSize(340, 92)
        self.setStyleSheet('background:#1a2233; border-radius:10px;')
        lay = QVBoxLayout(self)
        lab = QLabel('按下想要的组合键（需含 Ctrl/Alt/Shift 或 F 功能键）' + chr(10) + 'Esc 取消')
        lab.setStyleSheet('color:#ffffff; font-size:13px; background:transparent;')
        lab.setAlignment(Qt.AlignCenter)
        lay.addWidget(lab)
        self.result = None  # (mods, vk, 文本)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
            return
        if e.key() in (Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta, Qt.Key_AltGr):
            return
        mods = 0
        parts = []
        if e.modifiers() & Qt.ControlModifier:
            mods |= winrelay.MOD_CONTROL
            parts.append('Ctrl')
        if e.modifiers() & Qt.AltModifier:
            mods |= winrelay.MOD_ALT
            parts.append('Alt')
        if e.modifiers() & Qt.ShiftModifier:
            mods |= winrelay.MOD_SHIFT
            parts.append('Shift')
        is_f = Qt.Key_F1 <= e.key() <= Qt.Key_F35
        if mods == 0 and not is_f:
            return
        name = e.text().upper() or ('F' + str(e.key() - Qt.Key_F0))
        self.result = (mods, int(e.nativeVirtualKey()), '+'.join(parts + [name]))
        self.close()


class QuickFeedback(QWidget):
    '''游戏内快速反馈：底部小输入条（置顶无框），Enter 发送（带截图），Esc 取消。'''

    def __init__(self, console):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.console = console
        self.setFixedWidth(640)
        self.setStyleSheet('background:#1a2233; border-radius:12px;')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        head = QHBoxLayout()
        t1 = QLabel('🎮 快速反馈 — 当前画面已截取')
        t1.setStyleSheet('color:#ffffff; font-size:13px; font-weight:700; background:transparent;')
        self.thumb = QLabel()
        self.thumb.setFixedSize(150, 84)
        self.thumb.setStyleSheet('background:#0b0f1a; border:1px solid #33415c; border-radius:6px;')
        head.addWidget(t1)
        head.addStretch(1)
        head.addWidget(self.thumb)
        lay.addLayout(head)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText('描述你看到的 bug… Enter 发送给 AI，Esc 取消')
        self.edit.setStyleSheet('QLineEdit { background:#ffffff; border:none; '
            'border-radius:8px; padding:9px; font-size:14px; }')
        self.edit.returnPressed.connect(self._submit)
        lay.addWidget(self.edit)
        self._img = None
        self._game = None

    def popup(self, img, game_hwnd):
        self._img = img
        self._game = game_hwnd
        if img is not None:
            data = img.tobytes()
            qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888)
            self._buf = data
            self.thumb.setPixmap(QPixmap.fromImage(qimg).scaled(148, 82, Qt.KeepAspectRatio,
                                                              Qt.SmoothTransformation))
        self.edit.clear()
        g = QApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        self.move(g.left() + (g.width() - self.width()) // 2, g.bottom() - self.height() - 120)
        self.show()
        self.raise_()
        self.activateWindow()
        # 游戏窗口常年是"无边框全屏 + TOPMOST"，普通置顶窗口会压在它下面（实测：日志说
        # show 成功、屏幕上却什么都看不到）。所以：① 显式抬到 TOPMOST 带最前；
        # ② 拿焦点（不拿就没法打字）；③ 开一个定时器**持续**维持置顶——很多游戏每帧都会
        # 把自己重新抬到最前，只抬一次会被顶下去。
        try:
            hwnd = int(self.winId())
            u = winrelay.user32
            if game_hwnd:
                # 认游戏窗口当"拥有者"：Windows 保证被拥有的窗口永远排在自己的拥有者之上，
                # 这是压过"无边框全屏 + TOPMOST"游戏最可靠的一招（比反复抬 z 序稳）。
                try:
                    u.SetWindowLongW(hwnd, -8, int(game_hwnd))     # GWLP_HWNDPARENT
                except Exception:
                    pass
            u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)  # HWND_TOPMOST|SHOW
            winrelay.force_foreground(hwnd)
        except Exception:
            pass
        try:
            if getattr(self, "_top_timer", None) is None:
                self._top_timer = QTimer(self)
                self._top_timer.timeout.connect(self._keep_on_top)
            self._top_timer.start(350)
        except Exception:
            pass
        self.edit.setFocus()

    def _keep_on_top(self):
        """每 0.35 秒把输入条重新抬到 TOPMOST 最前（不抢焦点）。"""
        try:
            winrelay.user32.SetWindowPos(int(self.winId()), -1, 0, 0, 0, 0,
                                         0x0001 | 0x0002 | 0x0010 | 0x0040)  # NOSIZE|NOMOVE|NOACTIVATE|SHOW
            self._top_ticks = int(getattr(self, "_top_ticks", 0)) + 1
            if self._top_ticks in (1, 3, 10):
                from .reply import _reply_log

                _reply_log("输入条维持置顶 第%d次（visible=%s）" % (self._top_ticks, self.isVisible()))
        except Exception:
            pass

    def _stop_keep_on_top(self):
        try:
            if getattr(self, "_top_timer", None) is not None:
                self._top_timer.stop()
        except Exception:
            pass

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._stop_keep_on_top()
            self.hide()
            self._restore_game_focus()
        super().keyPressEvent(e)

    def _restore_game_focus(self):
        """收起输入条后把焦点还给游戏窗口（不然游戏一直处于失焦状态）。"""
        game = getattr(self, "_game", None)
        if game:
            try:
                winrelay.force_foreground(int(game))
            except Exception:
                pass

    def _submit(self):
        text = self.edit.text().strip()
        self._stop_keep_on_top()
        self.hide()
        game = self._game
        if game:
            try:
                winrelay.force_foreground(int(game))   # 立刻把焦点还给游戏，缩短离开时间
            except Exception:
                pass
        if text or self._img is not None:
            self.console._submit_feedback(text, self._img, game)


class ConsoleWindow(QWidget):
    """白底主界面：左输入右实时画面，顶部选 AI 客户端窗口。"""

    _relay_done = Signal(bool, str)
    _frame = Signal(object, int, int)  # 预览帧：RGB 字节流 + 宽高
    _guard_hit = Signal()              # 钩子线程 → GUI：拦到回车
    _guard_result = Signal(str, list, str)  # 分析线程 → GUI：当前行 + 问题列表 + 语言
    _reply_done = Signal(object, object, str)  # 回读线程 → GUI：回复文本 + 截图 + 模式(uia/ocr/none/timeout)
    _reply_tick = Signal(int)                  # 回读线程 → GUI：已等待秒数
    _reply_partial = Signal(str)
    _recover_done = Signal(str)                # 重启补读线程 → GUI：补回来的回复正文
    _client_launched = Signal()          # 启动线程 → GUI：客户端窗口已出现
    collapsed = Signal()                   # 控制台被关闭/收起 → 悬浮球回归
    task_done = Signal()                   # 任务完成（收到回复/队列发完）→ 从悬浮球弹出汇报
    open_fb = Signal()                     # 控制台工具栏按钮 → 打开快捷反馈条（热键的备份入口）
    _bg_send_done = Signal(bool, str, str)  # 后台发送线程 → GUI：ok/err/文本               # 回读线程 → GUI：生成中的部分文本（流式预览）

    def __init__(self):
        super().__init__(None, Qt.Window)
        self.setWindowTitle("竞眼雷达 · 屏幕助理")
        self.resize(1120, 660)
        self.setMinimumSize(960, 560)
        self.settings = QSettings("RivalEye", "Assistant")

        self.last_image = None   # 最近一次发送用的 PIL 图
        self._pixmap_buf = None  # QImage 构造期间的数据引用
        self.busy = False
        self.target_hwnd = 0

        # 代码守卫状态（钩子线程只读这些简单字段）
        self._guard_on = False
        self._guard_busy = False
        self._allow_enter_until = 0.0
        self._hook = None

        # 回复闭环状态
        self._watch_stop = None
        self._watching = False
        self._last_reply_text = ""
        self._pending_question = ""
        self._pending_baseline = None
        self._fb_queue = []      # 游戏模式：延迟发送的反馈队列
        self._fb_draining = False
        if getattr(sys, "frozen", False):
            _base = os.path.dirname(sys.executable)
        else:
            _base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._sess_dir = os.path.join(_base, "会话")
        os.makedirs(self._sess_dir, exist_ok=True)
        _latest = self._latest_session()
        self._hist_path = _latest or os.path.join(
            self._sess_dir, time.strftime("s%Y%m%d-%H%M%S") + str(int(time.time()*1000)%1000) + ".jsonl")

        # 实时预览状态（工作线程只读这些简单字段）
        self._visible = False
        self._live_on = True
        self._mon_index = 0
        self._live_stop = threading.Event()
        self._region = self._load_region()  # 选区（mss 物理坐标框），None = 整个画面源
        self._lock_hwnd = 0   # 锁定窗口捕获（OBS 式）；优先级：锁定 > 选区 > 显示器
        self._lock_title = ""

        self._relay_done.connect(self._on_relay_done)
        self._frame.connect(self._on_frame)
        self._guard_hit.connect(self._on_guard_hit)
        self._guard_result.connect(self._on_guard_result)
        self._reply_done.connect(self._on_reply)
        self._reply_tick.connect(self._on_reply_tick)
        self._reply_partial.connect(self._on_reply_partial)
        self._recover_done.connect(self._on_recovered)
        self._bg_send_done.connect(self._on_bg_send_done)
        self._client_launched.connect(self._on_client_launched)
        self._build_ui()
        self._refresh_windows()
        saved_lock = str(self.settings.value("lock_title", ""))
        if saved_lock:  # 按标题找回上次锁定的窗口（句柄跨会话失效）
            for h, t in winrelay.list_windows(exclude_titles=("竞眼雷达", "RivalEye")):
                if t == saved_lock:
                    self._lock_hwnd, self._lock_title = h, t
                    break
        self._set_region_label()
        if str(self.settings.value('autolaunch', '1')) == '1':
            _saved_t = str(self.settings.value('target_title', ''))
            _present = _saved_t and any(_saved_t in _t for _h, _t in
                                        winrelay.list_windows(exclude_titles=('竞眼雷达',)))
            if not _present and self._get_client_exe():
                threading.Thread(target=self._autolaunch, daemon=True).start()
        self._load_history()
        self._refresh_sessions()
        threading.Thread(target=self._live_loop, daemon=True, name="live-preview").start()

    # ---------- UI ----------
    def _build_ui(self):
        self.setStyleSheet(
            "ConsoleWindow { background:#ffffff; }"
            "QLabel { color:#374151; font-size:13px; background:transparent; border:none; }"
            "QComboBox { background:#ffffff; border:1px solid #d1d5db; border-radius:8px;"
            " padding:6px 10px; font-size:13px; }"
            "QSpinBox { border:1px solid #d1d5db; border-radius:6px; padding:3px 6px; }"
            "QCheckBox { color:#374151; font-size:13px; background:transparent; }"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 顶栏：标题 + 收起/退出
        top = QHBoxLayout()
        title = QLabel("📡 竞眼雷达 · 屏幕助理")
        title.setStyleSheet("font-size:16px; font-weight:700; color:#1a2233;")
        self.topmost = QCheckBox("置顶")
        self.topmost.toggled.connect(self._toggle_topmost)
        b_hide = QPushButton("收起")
        b_hide.setStyleSheet(_BTN_LIGHT)
        b_hide.clicked.connect(self.hide)
        b_quit = QPushButton("退出")
        b_quit.setStyleSheet(_BTN_LIGHT)
        b_quit.clicked.connect(QApplication.instance().quit)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(self.topmost)
        top.addWidget(b_hide)
        top.addWidget(b_quit)
        root.addLayout(top)

        # 工具栏：AI 客户端选择
        bar = QHBoxLayout()
        bar.addWidget(QLabel("AI 客户端："))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(360)
        self.combo.currentIndexChanged.connect(self._on_pick)
        bar.addWidget(self.combo, 1)
        b_refresh = QPushButton("刷新列表")
        b_refresh.setStyleSheet(_BTN_LIGHT)
        b_refresh.clicked.connect(self._refresh_windows)
        b_launch = QPushButton('🚀 启动客户端')
        b_launch.setStyleSheet(_BTN_LIGHT)
        b_launch.clicked.connect(self._launch_client)
        b_fb = QPushButton('📝 快速反馈')
        b_fb.setStyleSheet(_BTN_LIGHT)
        b_fb.setToolTip("和全局快捷键等价的入口：抓当前画面 + 弹底部输入条。\n"
                        "热键（默认 Ctrl+Alt+B）在某些全屏游戏/被占用时会失灵，用这个兜底。")
        b_fb.clicked.connect(self.open_fb.emit)
        b_bl = QPushButton('🎮 无边框化前台窗口')
        b_bl.setStyleSheet(_BTN_LIGHT)
        b_bl.setToolTip("先在游戏里把显示模式改成「窗口(Windowed)」，再点这个：\n"
                        "把前台游戏去掉边框、铺满屏幕（无边框窗口化）。\n"
                        "这样玩游戏时弹反馈条、打字、看回复都不会把游戏挤掉——\n"
                        "独占全屏做不到（一抢焦点就黑屏/掉回桌面，任何程序都一样）。")
        b_bl.clicked.connect(self._make_fg_borderless)
        bar.addWidget(b_bl)
        bar.addWidget(b_fb)
        bar.addWidget(b_launch)
        bar.addWidget(b_refresh)
        root.addLayout(bar)

        # 主体：左输入 / 右预览
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)

        # --- 左：会话栏 + 历史 + 输入 ---
        left = QSplitter(Qt.Horizontal)
        left.setChildrenCollapsible(False)
        sess = QWidget()
        sv = QVBoxLayout(sess)
        sv.setContentsMargins(0, 0, 8, 0)
        sv.setSpacing(6)
        b_newsess = QPushButton('＋ 新会话')
        b_newsess.setStyleSheet(_BTN_BLUE)
        b_newsess.clicked.connect(self._new_session)
        sv.addWidget(b_newsess)
        self.sess_list = QListWidget()
        self.sess_list.setStyleSheet(
            "QListWidget { background:#f8fafc; border:1px solid #e5e7eb; border-radius:10px;"
            " font-size:13px; color:#1f2937; padding:4px }"
            "QListWidget::item { padding:6px; border-radius:6px; }"
            "QListWidget::item:selected { background:#eef2ff; color:#4f46e5; }")
        self.sess_list.setSpacing(4)
        self.sess_list.itemClicked.connect(self._switch_session)
        sv.addWidget(self.sess_list, 1)
        left.addWidget(sess)
        chatw = QWidget()
        lv = QVBoxLayout(chatw)
        left.addWidget(chatw)
        left.setSizes([210, 800])
        lv.setContentsMargins(0, 0, 12, 0)
        lv.setSpacing(8)
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.history.setPlaceholderText("发送记录会显示在这里")
        try:
            self._hist_px = max(11, min(30, int(self.settings.value("hist_font_px", 15))))
        except Exception:
            self._hist_px = 15
        self._apply_hist_font()
        self.history.installEventFilter(self)
        for seq, delta in (("Ctrl+=", +1), ("Ctrl++", +1), ("Ctrl+-", -1)):
            try:
                sc = QShortcut(QKeySequence(seq), self)
                sc.activated.connect(lambda d=delta: self._hist_zoom(d))
            except Exception:
                pass
        try:
            sc0 = QShortcut(QKeySequence("Ctrl+0"), self)
            sc0.activated.connect(self._hist_reset_font)
        except Exception:
            pass
        lv.addWidget(self.history, 1)
        self.input = ChatInput()
        self.input.setPlaceholderText("输入要发给 AI 客户端的内容…（Enter 发送，Shift+Enter 换行）")
        self.input.setFixedHeight(96)
        self.input.setStyleSheet(
            "QTextEdit { background:#ffffff; border:1px solid #d1d5db; border-radius:10px;"
            " padding:8px; font-size:14px; color:#111827; }"
            "QTextEdit:focus { border:1px solid #5b8cff; }"
        )
        self.input.send_requested.connect(self._send)
        lv.addWidget(self.input)

        # 底栏拆两行：上面一行放开关，下面一行放按钮。
        # 挤在一行时 Qt 会把标签截成"附带截/发送/清空"这种半截字，两个字号按钮更会缩成小方块。
        opts_row = QHBoxLayout()
        opts_row.setSpacing(14)
        self.attach = QCheckBox("附带截图")
        self.attach.setChecked(True)
        self.attach.setToolTip("发送那一瞬间重新截取当前画面，连同文字一起发给 AI（默认开）")
        self.auto_enter = QCheckBox("发送后自动回车")
        self.auto_enter.setChecked(True)
        self.auto_enter.setToolTip("粘贴完自动按回车投递；关掉则只粘贴、由你手动回车")
        self.guard_box = QCheckBox("🛡 回车纠错")
        self.guard_box.setToolTip("锁定窗口为编辑器时：按回车前检查当前行语法，\n"
                                  "有硬错误则拦截并标注改法；3 秒内再按一次回车强制换行。\n"
                                  "支持 Python 与 C。")
        self.guard_box.toggled.connect(self._toggle_guard)
        self.guard_lang = QComboBox()
        self.guard_lang.addItem("自动", "auto")
        self.guard_lang.addItem("C", "c")
        self.guard_lang.addItem("Python", "python")
        self.guard_lang.setToolTip("纠错使用的语言规则。\n自动 = 按锁定窗口标题里的文件名判断（如 main.c → C，xxx.py → Python）")
        self.guard_lang.setCurrentIndex(int(self.settings.value("guard_lang_idx", 0)))
        self.guard_lang.currentIndexChanged.connect(
            lambda i: self.settings.setValue("guard_lang_idx", i))
        opts_row.addWidget(self.attach)
        opts_row.addWidget(self.auto_enter)
        opts_row.addWidget(self.guard_box)
        opts_row.addWidget(self.guard_lang)
        self.pop_front = QCheckBox("回复后自动弹出")
        self.pop_front.setToolTip("AI 回复到达时把控制台拉到最前、直达对话。\n"
                                  "不想被打扰就取消勾选（仍会弹托盘气泡提醒）。")
        self.pop_front.setChecked(str(self.settings.value("pop_front", "1")) == "1")
        self.pop_front.toggled.connect(
            lambda v: self.settings.setValue("pop_front", "1" if v else "0"))
        opts_row.addWidget(self.pop_front)
        opts_row.addStretch(1)

        act_row = QHBoxLayout()
        act_row.setSpacing(8)
        font_lab = QLabel("对话字号")
        font_lab.setStyleSheet("color:#6b7280; font-size:12px;")
        self.font_val = QLabel("%dpx" % int(getattr(self, "_hist_px", 15)))
        self.font_val.setAlignment(Qt.AlignCenter)
        self.font_val.setFixedWidth(44)
        self.font_val.setStyleSheet("color:#5b8cff; font-size:12px; font-weight:700;")
        b_font_minus = QPushButton("A-")
        b_font_minus.setStyleSheet(_BTN_TINY)
        b_font_minus.setFixedWidth(38)
        b_font_minus.setToolTip("对话字号变小（Ctrl+- 也行）")
        b_font_minus.clicked.connect(lambda: self._hist_zoom(-1))
        b_font_plus = QPushButton("A+")
        b_font_plus.setStyleSheet(_BTN_TINY)
        b_font_plus.setFixedWidth(38)
        b_font_plus.setToolTip("对话字号变大（Ctrl++ / 在对话区按住 Ctrl 滚轮 也行，Ctrl+0 复位）")
        b_font_plus.clicked.connect(lambda: self._hist_zoom(+1))
        b_clear = QPushButton("清空输入")
        b_newchat = QPushButton("🧹 清空会话")
        b_newchat.setStyleSheet(_BTN_LIGHT)
        b_newchat.clicked.connect(self._clear_session)
        b_clear.setStyleSheet(_BTN_LIGHT)
        b_clear.clicked.connect(self.input.clear)
        self.send_btn = QPushButton("发 送")
        self.send_btn.setStyleSheet(_BTN_BLUE)
        self.send_btn.setMinimumWidth(96)
        self.send_btn.setToolTip("发送到所选 AI 客户端（输入框里按 Enter 也是发送）")
        self.send_btn.clicked.connect(self._send)
        act_row.addWidget(font_lab)
        act_row.addWidget(b_font_minus)
        act_row.addWidget(self.font_val)
        act_row.addWidget(b_font_plus)
        act_row.addSpacing(16)
        act_row.addWidget(b_clear)
        act_row.addWidget(b_newchat)
        act_row.addStretch(1)
        act_row.addWidget(self.send_btn)
        lv.addLayout(opts_row)
        lv.addLayout(act_row)
        split.addWidget(left)

        # --- 右：实时画面（OBS 式） ---
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(12, 0, 0, 0)
        rv.setSpacing(8)
        cap_title = QLabel("实时画面")
        cap_title.setStyleSheet("font-weight:700; color:#1a2233;")
        rv.addWidget(cap_title)
        self.preview = QLabel("实时画面加载中…")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(240)
        self.preview.setStyleSheet(
            "background:#1a2233; color:#8b94a7; border-radius:10px; font-size:14px;"
        )
        rv.addWidget(self.preview, 1)
        mon_row = QHBoxLayout()
        mon_row.setSpacing(6)
        mon_row.addWidget(QLabel("画面源"))
        self.monitor_combo = QComboBox()
        self.monitor_combo.setMinimumWidth(150)
        self._fill_monitors()
        self.monitor_combo.currentIndexChanged.connect(self._on_monitor)
        mon_row.addWidget(self.monitor_combo, 1)
        self.live = QCheckBox("实时")
        self.live.setToolTip("实时预览（约 10fps，仅本地显示，不外发；收起窗口自动暂停）")
        self.live.setChecked(True)
        self.live.toggled.connect(self._set_live)
        mon_row.addWidget(self.live)
        rv.addLayout(mon_row)

        src_row = QHBoxLayout()
        b_lock = QPushButton("🪟 锁定窗口")
        b_lock.setStyleSheet(_BTN_LIGHT)
        b_lock.clicked.connect(self._lock_window)
        src_row.addWidget(b_lock)
        b_region = QPushButton("🔲 选取范围")
        b_region.setStyleSheet(_BTN_LIGHT)
        b_region.clicked.connect(self._pick_region)
        src_row.addWidget(b_region)
        b_reset = QPushButton("✕ 重置")
        b_reset.setStyleSheet(_BTN_LIGHT)
        b_reset.clicked.connect(self._clear_region)
        src_row.addWidget(b_reset)
        src_row.addStretch(1)
        rv.addLayout(src_row)
        hk_row = QHBoxLayout()
        hk_row.addWidget(QLabel('游戏反馈快捷键：'))
        self.hk_label = QLabel(self._hotkey_text_now())
        self.hk_label.setStyleSheet('color:#5b8cff; font-size:12px; font-weight:700; background:transparent;')
        b_rebind = QPushButton('改键')
        b_rebind.setStyleSheet(_BTN_LIGHT)
        b_rebind.clicked.connect(self._rebind_hotkey)
        hk_row.addWidget(self.hk_label, 1)
        hk_row.addWidget(b_rebind)
        self.game_mode = QCheckBox('游戏模式')
        self.game_mode.setChecked(False)
        self.game_mode.setToolTip('默认关=回车立即发送（发完焦点自动回游戏）；开=反馈先排队，等你离开游戏才发送（独占全屏怕黑屏再开）；\n关：回车后立即发送（会短暂切到 AI 客户端再切回）。')
        hk_row.addWidget(self.game_mode)
        rv.addLayout(hk_row)
        self.region_label = QLabel("")
        self.region_label.setStyleSheet("color:#5b8cff; font-size:12px; font-weight:600;")
        rv.addWidget(self.region_label)
        self._set_region_label()
        self.status = QLabel("先在上方选择要接入的 AI 客户端窗口")
        self.status.setStyleSheet("color:#6b7280; font-size:12px;")
        self.status.setWordWrap(True)
        rv.addWidget(self.status)
        ctx_row = QHBoxLayout()
        self.watch_reply = QCheckBox("自动读回回复")
        self.watch_reply.setChecked(True)
        self.watch_reply.setToolTip("发送后自动盯目标窗口：画面停止变化即回复完成，\n读回正文（或截图）到左侧对话记录。")
        self.ctx_box = QCheckBox("附带上下文")
        self.ctx_box.setToolTip("发送时在提问后附带：最近一次 AI 回复 + 最新雷达巡检报告摘要。\n"
                                "同一聊天窗连续对话时通常不需要（客户端本身有上下文）；\n"
                                "切换了新会话/新窗口时勾上它，上下文不断档。")
        ctx_row.addWidget(self.watch_reply)
        ctx_row.addWidget(self.ctx_box)
        ctx_row.addStretch(1)
        rv.addLayout(ctx_row)
        self.gen_preview = QLabel("")
        self.gen_preview.setWordWrap(True)
        self.gen_preview.setStyleSheet(
            "color:#374151; font-size:12px; background:#f8fafc; border:1px dashed #cbd5e1;"
            " border-radius:8px; padding:6px;")
        self.gen_preview.hide()
        rv.addWidget(self.gen_preview)
        rv.addWidget(QLabel(
            "实时画面约 10fps，仅本地预览不外发，收起窗口自动暂停。\n"
            "发送 = 激活所选 AI 客户端 → 粘贴发送瞬间的画面与文字 → 回车。\n"
            "提示：画面源选单个显示器 = 原生分辨率最清晰，AI 不易看错；\n"
            "「全部显示器」会把多屏压进一张图，小字会糊。Ctrl+Alt+S 随时唤起。"
        ))
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

    # ---------- 窗口列表 ----------
    def _autolaunch(self):
        time.sleep(1.0)
        if winrelay.launch_client(self._get_client_exe()):
            self._wait_client_window()

    def _get_client_exe(self):
        exe = str(self.settings.value('client_exe', ''))
        if exe and os.path.exists(exe):
            return exe
        exe = winrelay.find_client_exe()
        if exe:
            self.settings.setValue('client_exe', exe)
        return exe

    def _launch_client(self):
        exe = self._get_client_exe()
        if not exe:
            self.status.setText('未找到 AI 客户端程序（ZCode 等），可先手动打开再刷新列表')
            return
        if not winrelay.launch_client(exe):
            self.status.setText('启动 AI 客户端失败')
            return
        self.status.setText('正在启动 AI 客户端，等待窗口出现…')
        threading.Thread(target=self._wait_client_window, daemon=True).start()

    def _wait_client_window(self, timeout_s=36):
        saved = str(self.settings.value('target_title', ''))
        before = {h for h, _t in winrelay.list_windows(exclude_titles=('竞眼雷达', 'RivalEye'))}
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(1.5)
            wins = winrelay.list_windows(exclude_titles=('竞眼雷达', 'RivalEye'))
            fresh = [h for h, _t in wins if h not in before]
            if fresh or (saved and any(saved in t2 for _h, t2 in wins)):
                self._client_launched.emit()
                return
        self._client_launched.emit()

    def _on_client_launched(self):
        self._refresh_windows()
        saved = str(self.settings.value('target_title', ''))
        if saved:
            idx = self.combo.findText(saved)
            if idx > 0:
                self.combo.setCurrentIndex(idx)
                self.status.setText('AI 客户端已就绪：' + saved)
                reply.warm_up_uia(self.target_hwnd)
                return
        self.status.setText('AI 客户端已启动，请在上方选择目标窗口')

    def _refresh_windows(self):
        prev = self.combo.currentText()
        self.combo.blockSignals(True)
        self.combo.clear()
        windows = winrelay.list_windows(exclude_titles=("竞眼雷达", "RivalEye"))
        self.combo.addItem("— 选择 AI 客户端窗口 —", 0)
        for hwnd, title in windows:
            self.combo.addItem(title, int(hwnd))
        self.combo.blockSignals(False)
        saved = self.settings.value("target_title", "")

        def _match(want):
            """找回上次的目标窗口。Chromium/Electron 的窗口标题 = "会话名 — 应用名"，
            换了会话标题就全变了，所以匹配三级退让：全等 → 前缀 → 应用名。"""
            if not want:
                return -1
            i = self.combo.findText(want)
            if i > 0:
                return i
            i = next((k for k in range(1, self.combo.count())
                      if self.combo.itemText(k).startswith(str(want)[:24])), -1)
            if i > 0:
                return i
            tail = re.split(r"\s*[—–\-]\s*", str(want))[-1].strip()
            if len(tail) >= 4:
                i = next((k for k in range(1, self.combo.count())
                          if tail in self.combo.itemText(k)), -1)
            return i

        for want in (prev, saved):
            idx = _match(want)
            if idx > 0:
                self.combo.setCurrentIndex(idx)
                break
        self._on_pick()

    def _on_pick(self, *_):
        self.target_hwnd = int(self.combo.currentData() or 0)
        if self.target_hwnd:
            title = self.combo.currentText()
            self.settings.setValue("target_title", title)
            self.status.setText(f"已连接：{title}")
            self.status.setStyleSheet("color:#059669; font-size:12px; font-weight:600;")
            reply.warm_up_uia(self.target_hwnd)  # 触发 Chromium 系客户端预建无障碍树

    # ---------- 实时画面（OBS 式预览） ----------
    def _fill_monitors(self):
        self.monitor_combo.blockSignals(True)
        self.monitor_combo.clear()
        try:
            import mss

            with mss.mss() as sct:
                n = len(sct.monitors) - 1
        except Exception:
            n = 0
        self.monitor_combo.addItem("全部显示器", 0)
        for i in range(1, max(n, 1) + 1):
            self.monitor_combo.addItem(f"显示器 {i}" + ("（主）" if i == 1 else ""), i)
        self.monitor_combo.blockSignals(False)
        # 默认主显示器：单屏原生分辨率最清晰；「全部显示器」会把多屏拼合压缩、文字发糊
        idx = int(self.settings.value("mon_index", 1))
        pos = self.monitor_combo.findData(idx)
        self.monitor_combo.setCurrentIndex(pos if pos >= 0 else min(1, self.monitor_combo.count() - 1))
        self._mon_index = int(self.monitor_combo.currentData() or 0)

    def _on_monitor(self, *_):
        self._mon_index = int(self.monitor_combo.currentData() or 0)
        self.settings.setValue("mon_index", self._mon_index)
        self._clear_region()  # 切换画面源后旧选区坐标失效

    # ---------- 选区 ----------
    def _load_region(self):
        raw = self.settings.value("region", "")
        try:
            l, t, w, h = (int(v) for v in str(raw).split(","))
            if w > 0 and h > 0:
                return {"left": l, "top": t, "width": w, "height": h}
        except Exception:
            pass
        return None

    def _set_region_label(self):
        if self._lock_hwnd:
            self.region_label.setText(f"🔒 已锁定窗口：{self._lock_title} — 预览与发送只取该窗口（被遮挡也能截到）")
        elif self._region:
            self.region_label.setText(
                f"🎯 已选区域 {self._region['width']}×{self._region['height']}"
                f" @({self._region['left']},{self._region['top']}) — 预览与发送只取该范围")
        else:
            self.region_label.setText("")

    def _lock_window(self):
        """弹出窗口列表，锁定后实时预览与发送都用 PrintWindow 捕获该窗口。"""
        wins = winrelay.list_windows(exclude_titles=("竞眼雷达", "RivalEye", "RelayTarget"))
        if not wins:
            self.status.setText("没有找到可锁定的窗口")
            return
        from PySide6.QtWidgets import QInputDialog

        titles = [t for _h, t in wins]
        title, ok = QInputDialog.getItem(self, "锁定窗口", "选择要持续捕获的窗口：", titles, 0, False)
        if not ok or not title:
            return
        hwnd = next(h for h, t in wins if t == title)
        self._lock_hwnd, self._lock_title = hwnd, title
        self.settings.setValue("lock_title", title)
        self._region = None  # 锁定与选区互斥
        self.settings.setValue("region", "")
        self._set_region_label()
        self.status.setText(f"已锁定「{title}」：预览与发送只取该窗口，被遮挡/在其他屏也能截到")

    def _pick_region(self):
        """在当前画面源对应显示器上拖框选区。"""
        self._lock_hwnd, self._lock_title = 0, ""  # 选区与锁定互斥
        self.settings.setValue("lock_title", "")
        try:
            import mss

            with mss.mss() as sct:
                mons = sct.monitors
        except Exception as e:
            self.status.setText(f"无法枚举显示器：{e}")
            return
        screens = QApplication.screens()
        idx = max(self._mon_index, 1) - 1
        idx = min(idx, len(screens) - 1, len(mons) - 2)
        qscr = screens[idx]
        mon = mons[idx + 1]

        self.picker = RegionPicker(qscr, mon)
        self.picker.picked.connect(self._on_region_picked)
        self.picker.cancelled.connect(lambda: self.status.setText("已取消选区"))
        self.picker.show_picker()

    def _on_region_picked(self, box: dict):
        self._region = box
        self.settings.setValue("region", f"{box['left']},{box['top']},{box['width']},{box['height']}")
        self._set_region_label()
        self.status.setText(f"已选区域 {box['width']}×{box['height']}，发送与预览只取该范围")

    def _clear_region(self):
        self._region = None
        self.settings.setValue("region", "")
        self._lock_hwnd, self._lock_title = 0, ""
        self.settings.setValue("lock_title", "")
        if hasattr(self, "guard_box") and self.guard_box.isChecked():
            self.guard_box.setChecked(False)  # 锁定没了，守卫随之关闭
        self._set_region_label()

    def _set_live(self, on: bool):
        self._live_on = on

    def _live_loop(self):
        """工作线程：持续抓帧 → 信号送回 GUI 线程显示。约 10fps，窗口收起时自动休眠。"""
        import mss
        from PIL import Image

        target_dt = 0.1
        sct = None
        while not self._live_stop.is_set():
            if not (self._live_on and self._visible):
                time.sleep(0.2)
                continue
            t0 = time.time()
            try:
                if self._lock_hwnd:
                    img = winrelay.capture_window(self._lock_hwnd, PREVIEW_MAX_W)
                    if img is None:  # 窗口已关闭/最小化，稍后再试
                        time.sleep(0.3)
                        continue
                else:
                    if sct is None:
                        sct = mss.mss()
                    mons = sct.monitors
                    box = self._region or mons[min(self._mon_index, len(mons) - 1)]
                    raw = sct.grab(box)
                    img = Image.frombytes("RGB", raw.size, raw.rgb)
                if img.width > PREVIEW_MAX_W:
                    # 单屏缩幅小用 LANCZOS 保清晰；多屏拼合本就大幅压缩，用快速算法保帧率
                    resample = (Image.Resampling.LANCZOS if img.width <= 2600
                                else Image.Resampling.BILINEAR)
                    img = img.resize((PREVIEW_MAX_W, int(img.height * PREVIEW_MAX_W / img.width)),
                                     resample)
                self._frame.emit(img.tobytes(), img.width, img.height)
            except Exception:
                try:
                    if sct is not None:
                        sct.close()
                except Exception:
                    pass
                sct = None
                time.sleep(0.5)
                continue
            time.sleep(max(0.0, target_dt - (time.time() - t0)))

    def _on_frame(self, data, w: int, h: int):
        self._pixmap_buf = data  # QImage 不复制缓冲区，必须保活到下一帧
        qimg = QImage(data, w, h, w * 3, QImage.Format_RGB888)
        self.preview.setPixmap(QPixmap.fromImage(qimg).scaled(
            self.preview.width(), self.preview.height(),
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        ))

    def stop_live(self):
        self._live_stop.set()

    def showEvent(self, e):
        super().showEvent(e)
        self._visible = True

    def hideEvent(self, e):
        super().hideEvent(e)
        self._visible = False

    def _toggle_topmost(self, on: bool):
        self.hide()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()

    # ---------- 发送 ----------
    def _capture_for_send(self):
        try:
            if self._lock_hwnd:
                return winrelay.capture_window(self._lock_hwnd, SEND_IMAGE_MAX_W)
            if self._region:
                return grab_box_image(self._region, SEND_IMAGE_MAX_W)
            return grab_monitor_image(self._mon_index, SEND_IMAGE_MAX_W)
        except Exception:
            return None

    def _submit_feedback(self, text, img, game_hwnd):
        '''游戏反馈入口。优先级：游戏模式排队 > UIA 后台发送（零焦点） > 焦点中继兜底。'''
        if not text and img is None:
            return
        if self.game_mode.isChecked() and game_hwnd and winrelay.user32.IsWindow(game_hwnd):
            self._fb_queue.append((text, img))
            self.status.setText(f'反馈已排队（{len(self._fb_queue)} 条），离开游戏后自动发送')
            self._append('📝 反馈已排队，退出/切出游戏后自动发送：' + (text or '（仅截图）')[:40])
            tray = getattr(self, '_tray', None)
            if tray is not None:
                try:
                    tray.showMessage('竞眼雷达', '反馈已排队，离开游戏后自动发送')
                except Exception:
                    pass
            if not self._fb_draining:
                self._fb_draining = True
                threading.Thread(target=self._drain_feedback, args=(game_hwnd,), daemon=True).start()
            return
        if text or img is not None:
            # 极速中继：贴完立即还焦点给游戏（前台占用约 1.5 秒）；截图随行
            self.status.setText('极速发送中（约 1.5 秒后焦点还给你）…')
            hwnd2 = self.target_hwnd

            def work():
                baseline = reply.uia_snapshot(hwnd2) if self.watch_reply.isChecked() else None
                ok, err = winrelay.relay_to_window(hwnd2, text or '', image=img, send_enter=True,
                                                   restore_to=game_hwnd or None, fast=True)
                self._bg_send_done.emit(ok, err, text or '（仅截图）')
                self._pending_baseline = baseline

            threading.Thread(target=work, daemon=True, name='fast-send').start()
            return
        self._quick_send(text, img, restore_to=game_hwnd or None)

    def _on_bg_send_done(self, ok, err, text):
        self._pending_question = text
        if ok:
            self._append('🧑 ' + self._now() + '（游戏反馈·后台）' + text)
            self._persist('user', text)
            self._refresh_sessions()
            self.status.setText('后台已发送 ✅ 等待 AI 回复…')
            if self.watch_reply.isChecked() and self.target_hwnd:
                self._watch_reply()
        else:
            winrelay.set_clipboard_text(text)
            self.status.setText('后台发送失败（' + err + '），内容已复制到剪贴板')
            self._append('⚠ 后台发送失败：' + err + '（内容已复制）')

    def _drain_feedback(self, game_hwnd):
        import time as _t
        try:
            while self._fb_queue:
                # 等到游戏不再是前台（用户暂停/切出/退出），最多等 30 分钟
                deadline = _t.time() + 1800
                while (winrelay.user32.GetForegroundWindow() == game_hwnd
                       and winrelay.user32.IsWindow(game_hwnd) and _t.time() < deadline):
                    _t.sleep(1.0)
                text, img = self._fb_queue[0]
                # 等 UI 空闲再发（避免和上一次发送/监听打架）
                waited = 0
                while self.busy and waited < 300:
                    _t.sleep(1.0)
                    waited += 1
                self._fb_queue.pop(0)
                self._quick_send(text, img, restore_to=None)
                _t.sleep(8)  # 给回复监听留时间再发下一条
            if not self._fb_queue and not self.busy:
                self.task_done.emit()
        finally:
            self._fb_draining = False

    def _quick_send(self, text, image, restore_to=None):
        '''快速反馈通道：外部弹窗调用，带图发送到当前目标，焦点还给游戏窗口。'''
        if self.busy:
            self.status.setText('正在发送上一条，请稍候')
            return
        if not self.target_hwnd:
            self.status.setText('未选择 AI 客户端，快速反馈未发送（内容已复制）')
            if text:
                winrelay.set_clipboard_text(text)
            return
        payload = text
        self.busy = True
        self.send_btn.setEnabled(False)
        self.status.setText('正在发送快速反馈…')
        hwnd = self.target_hwnd
        self._pending_question = text
        self._append('🧑 ' + self._now() + '（游戏反馈）' + (text or '（仅截图）'))
        if text:
            self._persist('user', text)
            self._refresh_sessions()

        def work():
            baseline = reply.uia_snapshot(hwnd) if self.watch_reply.isChecked() else None
            ok, err = winrelay.relay_to_window(hwnd, payload, image=image, send_enter=True,
                                               restore_to=restore_to)
            self._relay_done.emit(ok, err)
            self._pending_baseline = baseline

        threading.Thread(target=work, daemon=True).start()

    def _hotkey_text_now(self):
        mods = int(self.settings.value('fb_mods', winrelay.MOD_CONTROL | winrelay.MOD_ALT))
        vk = int(self.settings.value('fb_vk', 0x42))
        parts = []
        if mods & winrelay.MOD_CONTROL:
            parts.append('Ctrl')
        if mods & winrelay.MOD_ALT:
            parts.append('Alt')
        if mods & winrelay.MOD_SHIFT:
            parts.append('Shift')
        if 0x70 <= vk <= 0x87:
            key = 'F' + str(vk - 0x6F)
        elif 32 <= vk < 127:
            key = chr(vk)
        else:
            key = 'Key' + str(vk)
        return '+'.join(parts + [key])

    def _rebind_hotkey(self):
        dlg = KeyGrabDialog()
        dlg.exec()
        if not dlg.result:
            return
        mods, vk, text = dlg.result
        winrelay.unregister_feedback_hotkey()
        if winrelay.register_hotkey(winrelay.HOTKEY_FEEDBACK_ID, mods, vk):
            self.settings.setValue('fb_mods', mods)
            self.settings.setValue('fb_vk', vk)
            self.hk_label.setText(text)
            self.status.setText('反馈快捷键已改为 ' + text)
        else:
            winrelay.register_hotkey(winrelay.HOTKEY_FEEDBACK_ID,
                                     winrelay.MOD_CONTROL | winrelay.MOD_ALT, 0x42)
            self.status.setText('该组合键被其他程序占用，请换一个')

    def _send(self):
        if self.busy:
            return
        self._cancel_watch()  # 新发送取消旧回复监听
        if not self.target_hwnd:
            self.status.setText("⚠ 请先在上方选择 AI 客户端窗口")
            self.status.setStyleSheet("color:#e5484d; font-size:12px;")
            return
        text = self.input.toPlainText().strip()
        image = None
        if self.attach.isChecked():
            try:
                if self._lock_hwnd:
                    image = winrelay.capture_window(self._lock_hwnd, SEND_IMAGE_MAX_W)
                elif self._region:
                    image = grab_box_image(self._region, SEND_IMAGE_MAX_W)
                else:
                    image = grab_monitor_image(self._mon_index, SEND_IMAGE_MAX_W)
                self.last_image = image
            except Exception:
                image = None  # 截图失败则只发文本
        if not text and image is None:
            self.status.setText("⚠ 输入为空，且未成功截取画面")
            return
        payload = text
        if text and self.ctx_box.isChecked():
            parts = []
            if self._last_reply_text:
                parts.append("【上次回复】" + self._last_reply_text[:600])
            try:
                from .agent import latest_report_text

                rep = latest_report_text()
                if rep and "暂无雷达报告" not in rep:
                    parts.append("【雷达报告摘要】" + rep[:600])
            except Exception:
                pass
            if parts:
                payload = text + "\n\n" + "\n".join(parts)
        self.busy = True
        self.send_btn.setEnabled(False)
        self.status.setText(f"正在发送至「{self.combo.currentText()}」…")
        hwnd = self.target_hwnd
        q = text
        self._pending_question = q
        auto_enter = self.auto_enter.isChecked()
        self._append(f"🧑 {self._now()}\n{q or '（仅截图）'}")
        if q:
            self._persist("user", q)

        def work():
            baseline = None
            if self.watch_reply.isChecked():
                baseline = reply.uia_snapshot(hwnd)  # 发送前文本快照（回复提取排除旧内容）
            ok, err = winrelay.relay_to_window(hwnd, payload, image=image, send_enter=auto_enter)
            self._relay_done.emit(ok, err)
            self._pending_baseline = baseline

        threading.Thread(target=work, daemon=True).start()

    def _on_relay_done(self, ok: bool, err: str):
        self.busy = False
        self.send_btn.setEnabled(True)
        if ok:
            suffix = "（含截图）" if self.attach.isChecked() and self.last_image else ""
            self._append(f"→ 已发送至「{self.combo.currentText()}」✅{suffix}")
            self.input.clear()
            self._refresh_sessions()
            if (self.watch_reply.isChecked() and self.target_hwnd
                    and (self._pending_question or self.last_image)):
                self.status.setText("已发送，正在盯目标窗口等回复…")
                self._watch_reply()
            else:
                self.status.setText("已发送")
        else:
            self._append(f"⚠ 发送失败：{err}")
            self.status.setText(f"发送失败：{err}")
            # 失败也可能只是"送达没确认"（目标刚切过会话、无障碍树还没渲染出新气泡）。
            # 这种时候照样监听一轮——闭环断在这里的代价，比多等一轮大得多。
            if (self.watch_reply.isChecked() and self.target_hwnd
                    and (self._pending_question or self.last_image)):
                self.status.setText("发送结果不确定，仍然盯着等回复…")
                self._watch_reply()

    # ---------- 回复闭环 ----------
    def _cancel_watch(self):
        if self._watch_stop is not None:
            self._watch_stop.set()
        self._watching = False

    # ---------- 重启补读：上次退出/被替换时掐断的那条回复 ----------
    def _fullscreen_foreground(self) -> bool:
        """前台是不是"独占全屏"的应用（游戏 / 全屏播放器）？

        判据要能区分"全屏游戏"和"最大化窗口"——否则浏览器一最大化就再也不弹了：
          · 窗口矩形铺满**整块屏幕**（≥99%，含任务栏区域）→ 最大化窗口只到工作区，通常 ≤95%；
          · 窗口**没有标题栏**（WS_CAPTION 为空）→ 全屏游戏基本都是无边框的；
          · `IsZoomed` 为真直接排除（那是最大化）。
        独占全屏一失焦就会掉成窗口模式（黑魂这类老引擎尤其明显），命中就坚决不抢焦点。
        """
        try:
            u = winrelay.user32
            fg = u.GetForegroundWindow()
            if not fg or fg == int(self.winId()):
                return False
            if u.IsZoomed(fg):
                return False
            style = u.GetWindowLongW(fg, -16)      # GWL_STYLE
            if style & 0x00C00000:                 # WS_CAPTION
                return False
            r = winrelay.window_bounds(fg)
            sw = u.GetSystemMetrics(0) or 1920
            sh = u.GetSystemMetrics(1) or 1080
            return (r.right - r.left) >= sw * 0.99 and (r.bottom - r.top) >= sh * 0.99
        except Exception:
            return False

    def _should_pop_front(self) -> bool:
        """回复到了要不要把控制台弹到最前？

        要——用户明确要求"结果出来直接弹界面直达对话"。三种不弹：
        ① 用户把"回复后自动弹出"关掉了；
        ② 窗口本来就可见**且已经在最前**（弹它没意义，还打断正在读的人）；
        ③ **前台是全屏应用（游戏）**——一抢焦点独占全屏就掉成窗口模式（用户实测踩过：
           把黑魂从全屏弄成了窗口）。
        """
        try:
            if str(self.settings.value("pop_front", "1")) != "1":
                return False
        except Exception:
            pass
        if self._fullscreen_foreground():
            return False
        try:
            if not self.isVisible():
                return True
            return winrelay.user32.GetForegroundWindow() != int(self.winId())
        except Exception:
            return True

    def _recover_last_reply(self):
        """程序退出（或被新版本替换重启）会掐断正在跑的回复监听，那条回复就永远丢了。
        启动时检查会话文件：如果最后一条用户消息后面没有 assistant，就补读一次。"""
        if not self.watch_reply.isChecked() or not self.target_hwnd:
            return
        try:
            import json

            rows = [json.loads(l) for l in open(self._hist_path, encoding="utf-8") if l.strip()]
        except Exception:
            return
        pending = ""
        for r in rows:
            if r.get("role") == "user":
                pending = str(r.get("text") or "")
            elif r.get("role") == "assistant":
                pending = ""
        if not pending.strip() or len(reply._norm(pending)) < 2:
            return
        self.status.setText("正在补上一次没读完的回复…")
        threading.Thread(target=self._recover_worker, args=(pending,), daemon=True,
                         name="reply-recover").start()

    def _recover_worker(self, question: str):
        hwnd = self.target_hwnd
        # ① 先试 DSH 会话直读（原始 Markdown，且不受"现在屏幕上显示的是哪个会话"影响）
        try:
            from . import dshstore

            if dshstore.available() and dshstore.looks_like_target(hwnd):
                txt, spath = dshstore.reply_after(question, title=winrelay.window_title(hwnd),
                                                  timeout=10, poll=1.0, settle=1)
                if txt and reply._valid_reply(txt):
                    reply._reply_log("补读：会话直读命中 %d 字" % len(txt))
                    self._recover_done.emit(txt)
                    return
        except Exception as e:
            reply._reply_log("补读：会话直读异常 %r" % (e,))
        prev = None
        for i in range(14):                     # 约 100 秒（可能这条回复还在生成）
            time.sleep(7)
            try:
                if not winrelay.user32.IsWindow(hwnd):
                    return
            except Exception:
                return
            try:
                txt = reply.extract_reply_text(hwnd, question, baseline=None, log=(i == 0))
            except Exception:
                txt = None
            if not txt or not reply._valid_reply(txt):
                prev = None
                continue
            # 两重保险：① 客户端自己说没在生成；② 连续两次读数一致。
            # 否则会把"AI 调用工具前的旁白"当成回复补进来（13:50 实测踩过）。
            try:
                busy = reply._generating_now(hwnd)
            except Exception:
                busy = False
            if busy:
                prev = None
                continue
            if prev is not None and reply._norm(prev) == reply._norm(txt):
                self._recover_done.emit(txt)
                return
            prev = txt
        reply._reply_log("补读：没找到上一次那条提问的回复（窗口可能已切会话）")

    def _on_recovered(self, text: str):
        self._watching = False
        try:
            self._append_plain("🤖 " + self._now() + "（重启后补回）")
        except Exception:
            pass
        try:
            self._append_md(text)
        except Exception:
            try:
                self._append(text)
            except Exception:
                pass
        try:
            self._persist("assistant", text)
        except Exception:
            pass
        self.status.setText("已补回上次没读完的回复 ✅")
        # 补读是"事后补账"，不是实时回复——不弹窗打扰（用户要的是"回复到达时弹"）。

    def _watch_reply(self):
        """隔窗盯目标客户端：流式预览 + 生成结束读回正文（AI 长思考/工具调用不误判）。"""
        self._cancel_watch()
        hwnd = self.target_hwnd
        sent = self._pending_question or ""
        stop = threading.Event()
        self._watch_stop = stop
        self._watching = True

        def work():
            text, img, mode = reply.stream_reply(
                hwnd, sent, baseline=self._pending_baseline, stop=stop,
                progress=lambda s: self._reply_tick.emit(s),
                partial_cb=lambda s2: self._reply_partial.emit(s2))
            if stop.is_set():
                return
            self._reply_done.emit(text, img, mode)

        threading.Thread(target=work, daemon=True, name="reply-watch").start()


    def _on_reply_tick(self, secs: int):
        if self._watching:
            if str(getattr(reply, "LAST_REASON", "")) == "switched":
                self.status.setText("⚠ 目标窗口已切到别的会话，本轮回复读不到——切回去会自动补上（%ds）" % secs)
            else:
                self.status.setText(f"AI 生成中… {secs}s（再次发送可取消）")

    def _on_reply_partial(self, text: str):
        if self._watching and text:
            self.gen_preview.setText('🤖 生成中…\n' + text[-220:])
            self.gen_preview.show()

    def _on_reply(self, text, img, mode: str):
        self._watching = False
        from .reply import _reply_log
        try:
            self.gen_preview.hide()
            self.gen_preview.setText('')
        except Exception:
            pass
        if mode == 'cancelled':
            return
        if mode == 'timeout':
            self._append('🤖 （4 分钟内未检测到新回复；可手动查看目标窗口）')
            self.status.setText('未检测到回复（超时）')
            return
        if text:
            self._last_reply_text = text
            tag = {'ocr': 'OCR 识别', 'store': '会话直读'}.get(mode, '无障碍树')
            try:
                self._append_plain('🤖 ' + self._now() + '（' + tag + '）')
            except Exception as e:
                _reply_log('UI步骤异常(表头): ' + repr(e)[:80])
            try:
                self._append_md(text)
            except Exception:
                try:
                    self._append(text)
                except Exception:
                    pass
            try:
                self._persist('assistant', text)
            except Exception:
                pass
            self.status.setText('已收到回复 ✅')
            _reply_log('UI: 回复已入历史 ' + str(len(text)) + ' 字，isVisible=' + str(self.isVisible()))
            # 回复到达 → 把控制台拉到前台直达对话。
            # 旧逻辑只在窗口"隐藏"时才弹，所以窗口开着但被别的窗口压住时，用户只看到托盘气泡。
            # 把判定结果写进日志：下次"没弹出来"能直接从日志看出是哪一步的判断。
            try:
                pop = self._should_pop_front()
                _reply_log('弹窗判定 pop=%s｜窗口可见=%s｜前台==控制台=%s'
                           % (pop, self.isVisible(),
                              winrelay.user32.GetForegroundWindow() == int(self.winId())))
            except Exception:
                pop = True
            if pop:
                self.task_done.emit()
            _tray = getattr(self, '_tray', None)
            if _tray is not None:
                try:
                    _tray.showMessage('竞眼雷达 · AI 已回复', text[:80])
                except Exception:
                    pass
        else:
            if mode == 'noanchor':
                self._append_plain('🤖 ' + self._now() + '（没找到本轮提问 → 目标窗口很可能被切到别的会话了）')
                self.status.setText('没读到回复：目标窗口换了会话，请切回本会话再发')
                return
            self._append_plain('🤖 ' + self._now() + '（文本提取失败，回填截图）')
            try:
                self._append_image(img)
            except Exception:
                pass
            self.status.setText('已收到回复（截图兜底）')

    def _append_image(self, pil_img):
        from PIL import Image as PILImage

        w_target = 520
        if pil_img.width > w_target:
            pil_img = pil_img.resize((w_target, int(pil_img.height * w_target / pil_img.width)),
                                     PILImage.Resampling.BILINEAR)
        data = pil_img.tobytes()
        self._img_buf = data  # QImage 不复制缓冲区，保活
        qimg = QImage(data, pil_img.width, pil_img.height, pil_img.width * 3, QImage.Format_RGB888)
        self.history.moveCursor(QTextCursor.End)
        self.history.textCursor().insertImage(qimg)
        self.history.insertPlainText("\n\n")
        self._scroll_bottom()

    # ---------- 代码守卫（回车纠错） ----------
    def _toggle_guard(self, on: bool):
        if on and not self._lock_hwnd:
            self.guard_box.setChecked(False)
            self.status.setText("⚠ 回车纠错需要先「🪟 锁定窗口」到你的编辑器（如 VSCode）")
            return
        self._guard_on = on
        if on:
            self._hook = guard.LowLevelKeyboardHook(
                on_enter=self._guard_hit.emit, should_block=self._guard_should_block)
            self._hook.start()
            self.status.setText(f"🛡 回车纠错已开启（盯「{self._lock_title}」）：回车前检查当前行")
        else:
            if self._hook:
                self._hook.stop()
                self._hook = None
            self.status.setText("🛡 回车纠错已关闭")

    def _stop_guard(self):
        self._guard_on = False
        if self._hook:
            self._hook.stop()
            self._hook = None

    def _guard_should_block(self) -> bool:
        """钩子线程调用：只做标志位与前台判断，必须极快。"""
        return (self._guard_on and self._lock_hwnd and not self._guard_busy
                and time.time() >= self._allow_enter_until
                and winrelay.user32.GetForegroundWindow() == self._lock_hwnd)

    def _on_guard_hit(self):
        if self._guard_busy:
            return
        self._guard_busy = True

        def work():
            lang = self._guard_lang_now()
            line = guard.read_current_line(self._lock_hwnd)
            issues = guard.check_line(line, lang)
            self._guard_result.emit(line, issues, lang)

        threading.Thread(target=work, daemon=True, name="guard-analyze").start()

    def _guard_lang_now(self) -> str:
        lang = str(self.guard_lang.currentData() or "auto")
        if lang == "auto":
            return guard.detect_lang(self._lock_title)
        return lang

    def _on_guard_result(self, line: str, issues: list, lang: str):
        self._guard_busy = False
        if winrelay.user32.GetForegroundWindow() != self._lock_hwnd:
            return  # 分析期间用户已切走，放弃本次
        hard = [it for it in issues if it["block"]]
        if not hard:
            if issues:  # 软问题（如括号未闭合，多为故意续行）：提示但放行
                self._show_guard_tip(line, issues, lang)
            winrelay.send_enter_key()  # 干净/软问题：注入回车无感放行（钩子对注入键放行）
        else:
            self._allow_enter_until = time.time() + 3.0
            self._show_guard_tip(line, issues, lang)

    def _show_guard_tip(self, line: str, issues: list, lang: str):
        if not hasattr(self, "guard_tip"):
            self.guard_tip = GuardTip()
            self.guard_tip.allow_requested.connect(self._guard_allow)
        r = winrelay.window_bounds(self._lock_hwnd)
        x = r.left + max(8, (r.right - r.left - self.guard_tip.width()) // 2)
        y = max(r.top + 8, r.bottom - self.guard_tip.height() - 90)
        html_text = ("<div style='color:#e5484d;font-weight:700;font-size:13px'>"
                     f"🛡 代码守卫 · {lang.upper()} 行检查</div>"
                     + guard.render_line_html(line, issues))
        self.guard_tip.popup(html_text, x, y)

    def _guard_allow(self):
        self._allow_enter_until = time.time() + 3.0
        if winrelay.user32.GetForegroundWindow() == self._lock_hwnd:
            winrelay.send_enter_key()

    # ---------- 杂项 ----------
    @staticmethod
    def _now() -> str:
        import datetime

        return datetime.datetime.now().strftime("%H:%M:%S")

    def _append_plain(self, text: str):
        self._append(text)

    def _append_md(self, md_text: str):
        """AI 回复按 Markdown 渲染（代码块/表格/加粗如聊天界面）。"""
        from PySide6.QtGui import QTextDocument

        doc = QTextDocument()
        doc.setMarkdown(md_text)
        html = doc.toHtml()
        # Qt 的 markdown→HTML 会在 <body> 上写死 font-size（实测 9pt）——不清掉的话，
        # 对话区调字号时这些内容**永远**不会跟着变（用户实测："并没有做到即时变化"）。
        html = re.sub(r"font-size\s*:\s*[^;\"']+;?", "", html)
        cur = self.history.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertBlock()
        cur.insertHtml(html)
        cur.insertBlock()
        self._scroll_bottom()          # 回复很长时自动到底，不用手动拖

    def _persist(self, role: str, text: str):
        import datetime
        import json

        try:
            with open(self._hist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": datetime.datetime.now().isoformat(timespec="seconds"),
                                    "role": role, "text": text}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _load_history(self):
        import json
        import os

        try:
            if not os.path.exists(self._hist_path):
                return
            rows = []
            with open(self._hist_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue          # 坏行跳过：一行坏了不能把整段历史都吞掉
            rows = rows[-60:]
        except Exception:
            return
        for r in rows:
            if r.get("role") == "assistant":
                self._append_plain("🤖 " + str(r.get("t", ""))[11:16])
                self._append_md(str(r.get("text", "")))
            else:
                self._append_plain("🧑 " + str(r.get("t", ""))[11:16] + "\n" + str(r.get("text", "")))

    def _latest_session(self):
        import glob
        files = sorted(glob.glob(os.path.join(self._sess_dir, 's*.jsonl')), reverse=True)
        return files[0] if files else None

    def _session_label(self, path):
        import json
        name = os.path.basename(path)
        try:
            stamp = name[5:7] + '-' + name[7:9] + ' ' + name[10:12] + ':' + name[12:14]
        except Exception:
            stamp = name
        first = ''
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    r = json.loads(line)
                    if r.get('role') == 'user':
                        first = str(r.get('text', '')).replace(chr(10), ' ').strip()
                        break
        except Exception:
            pass
        if len(first) < 4:
            title = '新对话'
        else:
            title = first[:16] + ('…' if len(first) > 16 else '')
        return title, stamp

    def _refresh_sessions(self):
        import glob
        self.sess_list.blockSignals(True)
        self.sess_list.clear()
        for f in sorted(glob.glob(os.path.join(self._sess_dir, 's*.jsonl')), reverse=True)[:30]:
            _title, _stamp = self._session_label(f)
            it = QListWidgetItem(_title + chr(10) + _stamp)
            it.setData(1, f)
            it.setSizeHint(QSize(190, 48))
            self.sess_list.addItem(it)
            if os.path.abspath(f) == os.path.abspath(self._hist_path):
                self.sess_list.setCurrentItem(it)
        self.sess_list.blockSignals(False)

    def _new_session(self):
        self._hist_path = os.path.join(
            self._sess_dir, time.strftime('s%Y%m%d-%H%M%S') + str(int(time.time()*1000)%1000) + '.jsonl')
        open(self._hist_path, 'w', encoding='utf-8').close()
        self.history.clear()
        self._last_reply_text = ''
        self._refresh_sessions()
        self.status.setText('已开启新会话')

    def _switch_session(self, item):
        path = item.data(1)
        if not path or os.path.abspath(path) == os.path.abspath(self._hist_path):
            return
        self._hist_path = path
        self.history.clear()
        self._load_history()
        _title, _s2 = self._session_label(path)
        self.status.setText('已切换会话：' + _title)


    def _clear_session(self):
        import os

        self.history.clear()
        try:
            if os.path.exists(self._hist_path):
                os.remove(self._hist_path)
        except Exception:
            pass
        self._last_reply_text = ""
        self.status.setText("会话已清空")

    def _make_fg_borderless(self):
        """把前台窗口（通常是游戏）做成无边框全屏。前提：游戏已切成"窗口"模式。"""
        try:
            hwnd = winrelay.user32.GetForegroundWindow()
        except Exception:
            hwnd = 0
        if not hwnd or hwnd == int(self.winId()):
            self.status.setText("请先把游戏切到前台（并且显示模式设为「窗口」），再点这个按钮")
            return
        title = winrelay.window_title(hwnd)[:30]
        if winrelay.make_borderless(hwnd):
            reply._reply_log("已无边框化前台窗口：%r" % title)
            self.status.setText("已把「%s」变成无边框全屏 ✅ 现在玩游戏时弹窗不会再挤掉它" % title)
            self.status.setStyleSheet("color:#059669; font-size:12px; font-weight:600;")
        else:
            self.status.setText("无边框化失败（先把游戏显示模式改成「窗口」再试）")

    def _append(self, text: str):
        self.history.moveCursor(QTextCursor.End)
        self.history.insertPlainText(text + "\n\n")
        self._scroll_bottom()

    # ---------- 对话区字号 / 自动滚动 ----------
    def _apply_hist_font(self, rerender: bool = False):
        """对话区字号（px）。存 QSettings，重启后保留；A- / A+ / Ctrl+加减 / Ctrl+滚轮都能调。

        QTextEdit 的样式表只影响**之后插入**的内容，已经在里面的文字不会跟着变——
        所以调字号时要把整个对话区按新字号重灌一遍（rerender=True），效果才是即时的。
        """
        px = int(getattr(self, "_hist_px", 15))
        self.history.setStyleSheet(
            "QTextEdit { background:#ffffff; border:1px solid #e5e7eb; border-radius:10px;"
            " padding:10px; font-size:%dpx; color:#1f2937; }" % px
        )
        try:
            from PySide6.QtGui import QFont

            f = self.history.font()
            f.setPixelSize(px)
            self.history.setFont(f)
            self.history.document().setDefaultFont(f)   # 文档默认字体变了会立刻重排
        except Exception:
            pass
        fv = getattr(self, "font_val", None)
        if fv is not None:
            try:
                fv.setText("%dpx" % px)
            except Exception:
                pass
        try:
            self.settings.setValue("hist_font_px", px)
        except Exception:
            pass
        if rerender:
            self._reload_history_view()

    def _reload_history_view(self):
        """按当前字号重排对话区。**先确认读得到内容再清空**——读不到就保持原样，
        避免"一次读取失败把整屏历史刷没"（用户实测踩过：会话文件里其实还在，屏幕上却空了）。"""
        try:
            import json

            rows = []
            if os.path.exists(self._hist_path):
                with open(self._hist_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            continue
            if not rows:
                return
            self.history.clear()
            for r in rows[-60:]:
                if r.get("role") == "assistant":
                    self._append_plain("🤖 " + str(r.get("t", ""))[11:16])
                    self._append_md(str(r.get("text", "")))
                else:
                    self._append_plain("🧑 " + str(r.get("t", ""))[11:16] + "\n" + str(r.get("text", "")))
            self._scroll_bottom()
        except Exception:
            pass

    def _restyle_document(self, px: int):
        """给**已经显示的全部文字**就地套上新字号（px→pt 按 96dpi 近似换算）。

        为什么不"清空重灌"：重灌要先清屏再从会话文件读回来，一旦这次读取失败（文件被外部
        改写、坏行、路径切换…），屏幕上就只剩空气了——用户实测踩过"调个字号把回复刷没了"。
        就地改格式不依赖任何外部文件，改完即生效。
        """
        try:
            from PySide6.QtGui import QTextCharFormat

            cur = self.history.textCursor()
            pos = cur.position()
            cur.select(QTextCursor.Document)
            fmt = QTextCharFormat()
            fmt.setFontPointSize(max(6.0, round(px * 0.75, 1)))
            cur.mergeCharFormat(fmt)
            cur.clearSelection()
            cur.setPosition(pos)
            self.history.setTextCursor(cur)
        except Exception:
            pass

    def _hist_zoom(self, delta: int):
        self._hist_px = max(11, min(30, int(getattr(self, "_hist_px", 15)) + delta))
        self._apply_hist_font()
        self._restyle_document(self._hist_px)      # 就地生效，不重灌、不丢内容
        try:
            self.status.setText("对话字号：%d px（A-/A+ 或 Ctrl+加减可调，Ctrl+0 复位）" % self._hist_px)
        except Exception:
            pass
        self._scroll_bottom()

    def _hist_reset_font(self):
        self._hist_px = 15
        self._apply_hist_font()
        self._restyle_document(self._hist_px)
        self._scroll_bottom()

    def _scroll_bottom(self):
        """新内容进来后自动滚到底——回复是长文时不用手动往下拖。"""
        try:
            self.history.moveCursor(QTextCursor.End)
            sb = self.history.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception:
            pass

    def eventFilter(self, obj, ev):
        """Ctrl+滚轮 = 调对话字号（鼠标停在对话区就能用）。"""
        try:
            if obj is self.history and ev.type() == QEvent.Wheel and (ev.modifiers() & Qt.ControlModifier):
                self._hist_zoom(1 if ev.angleDelta().y() > 0 else -1)
                return True
        except Exception:
            pass
        return super().eventFilter(obj, ev)

    def closeEvent(self, e):
        e.ignore()
        self.hide()  # 关闭仅收起到悬浮球

    def hideEvent(self, e):
        super().hideEvent(e)
        self._visible = False
        self.collapsed.emit()  # 收起时悬浮球回归


class HotkeyFilter(QAbstractNativeEventFilter):
    """全局热键 Ctrl+Alt+S → 随时截取；单实例唤醒消息 → 弹出控制台。"""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, eventType, message):
        try:
            et = bytes(eventType)
            if et in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                msg = w.MSG.from_address(int(message))
                if msg.message == winrelay.WM_HOTKEY:
                    reply._reply_log("热键消息 wParam=%s（%s）" % (msg.wParam, et.decode()))
                    if msg.wParam in (winrelay.HOTKEY_ID, winrelay.HOTKEY_FEEDBACK_ID):
                        self.callback(int(msg.wParam))
                elif msg.message == winrelay.show_console_message():
                    self.callback(None)
        except Exception as e:
            reply._reply_log("热键处理异常: %r" % (e,))
        return False


class OverlayApp(QObject):
    _show_console = Signal()
    _open_fb = Signal(int)              # 钩子线程 → GUI：游戏反馈热键（携带当前前台窗口）

    def __init__(self, agent=None):  # agent 兼容旧签名（内嵌 VLM 聊天已由 Tk 后备保留）
        QObject.__init__(self)
        self.app = QApplication.instance() or QApplication(["竞眼雷达"])
        self.app.setQuitOnLastWindowClosed(False)  # 收起到球/托盘不退出
        self.console = ConsoleWindow()
        self.ball = BallWidget()
        self.ball.setToolTip("竞眼雷达 · 单击打开控制台，右键退出")
        self.ball.clicked.connect(self.toggle_console)
        self._show_console.connect(self.show_console)
        g = self.app.primaryScreen().availableGeometry()
        self.ball.move(g.right() - 64 - 40, g.bottom() - 64 - 90)
        self._build_tray()

        self.hotkey_ok = winrelay.register_capture_hotkey()
        fb_mods = int(self.console.settings.value('fb_mods', winrelay.MOD_CONTROL | winrelay.MOD_ALT))
        fb_vk = int(self.console.settings.value('fb_vk', 0x42))
        self._fb_hotkey_ok = winrelay.register_hotkey(winrelay.HOTKEY_FEEDBACK_ID, fb_mods, fb_vk)
        # RegisterHotKey 在全屏游戏里常被吞（实测黑魂全屏时 Ctrl+Alt+B 到不了应用），
        # 再挂一个**低级键盘钩子**兜底：它在系统分发按键之前就能看到，游戏拦不住。
        self._combo_hook = None
        try:
            self._combo_hook = guard.ComboHook(
                fb_vk,
                bool(fb_mods & winrelay.MOD_CONTROL),
                bool(fb_mods & winrelay.MOD_ALT),
                bool(fb_mods & winrelay.MOD_SHIFT),
                lambda: self._fire_fb_hotkey("钩子"),
            )
            self._combo_hook.start()
            reply._reply_log("组合键钩子已挂上（%s+%s）" % (fb_mods, fb_vk))
        except Exception as e:
            reply._reply_log("组合键钩子挂载失败：%r" % (e,))
        self._filter = HotkeyFilter(self._on_hotkey)
        self.app.installNativeEventFilter(self._filter)
        self._open_fb.connect(self._open_feedback)
        self.console.collapsed.connect(self._collapse)
        self.console.task_done.connect(self._on_task_done)
        try:
            self.console.open_fb.connect(
                lambda: self._open_feedback(int(winrelay.user32.GetForegroundWindow())))
        except Exception:
            pass
        # 把热键注册结果记进日志（失败时也在界面提示一句）——不然"按不出来"永远无从查起
        try:
            combo = "%s+%s" % (fb_mods, fb_vk)
            reply._reply_log("热键注册：截图 Ctrl+Alt+S=%s｜快速反馈 %s(id=%s)=%s"
                             % (self.hotkey_ok, combo, winrelay.HOTKEY_FEEDBACK_ID, self._fb_hotkey_ok))
            if not self._fb_hotkey_ok:
                self.console.status.setText(
                    "⚠ 快捷键 %s 注册失败（可能被别的程序占用）——请用控制台右上角「📝 快速反馈」，或点「改键」换一个组合"
                    % combo)
                self.console.status.setStyleSheet("color:#e5484d; font-size:12px;")
        except Exception:
            pass
        self.app.aboutToQuit.connect(winrelay.unregister_capture_hotkey)
        self.app.aboutToQuit.connect(winrelay.unregister_feedback_hotkey)
        self.app.aboutToQuit.connect(self.console.stop_live)
        self.app.aboutToQuit.connect(self.console._stop_guard)
        try:
            if getattr(self, "_combo_hook", None) is not None:
                self.app.aboutToQuit.connect(self._combo_hook.stop)
        except Exception:
            pass

    def _build_tray(self):
        """托盘图标：常驻可见，双击打开控制台，右键菜单退出。"""
        from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon

        icon = QIcon(sys.executable) if getattr(sys, "frozen", False) else QIcon()
        if icon.isNull() or icon.availableSizes() == []:
            pm = QPixmap(64, 64)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(QColor("#1a2233"))
            p.setPen(QColor("#5b8cff"))
            p.drawRoundedRect(2, 2, 60, 60, 18, 18)
            p.setPen(QColor("#ffffff"))
            f = QFont()
            f.setPixelSize(30)
            p.setFont(f)
            p.drawText(pm.rect(), 0x0004 | 0x0080, "📡")  # AlignCenter
            p.end()
            icon = QIcon(pm)

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("竞眼雷达 · 双击打开控制台")
        menu = QMenu()
        menu.addAction("打开控制台", self.show_console)
        menu.addSeparator()
        menu.addAction("退出竞眼雷达", self.app.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.show_console()
            if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger)
            else None
        )
        self.tray.show()
        self.console._tray = self.tray

    def _fire_fb_hotkey(self, source: str = ""):
        """快捷反馈热键的统一入口（RegisterHotKey 与低级钩子两条路都会走到这里，去重）。"""
        now = time.time()
        if now - getattr(self, "_last_fb_fire", 0.0) < 0.45:
            return
        self._last_fb_fire = now
        try:
            reply._reply_log("快捷反馈热键触发（来源：%s）" % (source or "热键消息"))
        except Exception:
            pass
        try:
            game = winrelay.user32.GetForegroundWindow()
        except Exception:
            game = 0
        self._open_fb.emit(int(game))

    def _on_hotkey(self, wparam=None):
        if wparam == winrelay.HOTKEY_FEEDBACK_ID:
            self._fire_fb_hotkey("RegisterHotKey")
            return
        self._show_console.emit()  # 跨线程安全地弹到 GUI 线程

    def _on_task_done(self):
        '''结果出现：悬浮窗/托盘已提示，1 秒后弹出界面直达对话。'''
        QTimer.singleShot(1000, self.show_console)

    def _open_feedback(self, game_hwnd):
        try:
            img = self.console._capture_for_send()
            reply._reply_log("打开快捷反馈条：截图=%s"
                             % ("无" if img is None else "%dx%d" % (img.width, img.height)))
            if getattr(self, "quick_fb", None) is None:
                self.quick_fb = QuickFeedback(self.console)
                # 关键：脱离控制台这个父窗口。Qt.Tool 子窗口会跟着父窗口一起隐藏，
                # 控制台一最小化（玩游戏的常态），反馈条就再也弹不出来——实测踩过。
                self.quick_fb.setParent(None)
            self.quick_fb.popup(img, game_hwnd or None)
            reply._reply_log("快捷反馈条 show() 完成，visible=%s" % self.quick_fb.isVisible())
        except Exception as e:
            reply._reply_log("打开快捷反馈条失败：%r" % (e,))
            try:
                self.console.status.setText("快捷反馈条打开失败：%r（用控制台里的输入框也能发）" % (e,))
            except Exception:
                pass

    def show_console(self):
        self.ball.hide()
        self.console.show()
        self.console.raise_()
        self.console.activateWindow()
        try:
            hwnd = int(self.console.winId())
        except Exception:
            hwnd = 0
        if not hwnd:
            return
        # 后台进程的 activateWindow 会被前台锁拒绝——交给 winrelay 的三重置前手段
        winrelay.force_foreground(hwnd)
        # 刚从"收起"状态弹回来时，Qt 的 show()/activateWindow() 是异步的：立刻抢前台会失败
        # （实测：窗口可见了，前台仍留在客户端）。补三次，越往后越稳。
        for delay in (250, 800, 1600):
            try:
                QTimer.singleShot(delay, lambda h=hwnd: self._reassert_front(h))
            except Exception:
                pass

    def _reassert_front(self, hwnd):
        """再抢一次前台（给 show() 之后的前台锁留点时间）。"""
        try:
            if not self.console.isVisible():
                return
            self.console.raise_()
            winrelay.force_foreground(hwnd)
        except Exception:
            pass

    def toggle_console(self):
        if self.console.isVisible():
            self._collapse()
        else:
            self.show_console()

    def _collapse(self):
        self.console.hide()
        self.ball.show()
        self.ball.raise_()

    def run(self):
        self.ball.show()
        self.show_console()  # 双击启动直接给白底控制台，不让用户找 64px 小球
        if not self.hotkey_ok:
            self.console.status.setText("⚠ Ctrl+Alt+S 热键注册失败（可能被占用），请用界面按钮截取")
        # 启动 3 秒后补读：上次退出/被新版本替换时掐断的那条回复（会话文件里能看出缺口）
        try:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(3000, self.console._recover_last_reply)
        except Exception:
            pass
        return self.app.exec()


_BTN_LIGHT = (
    "QPushButton { background:#ffffff; color:#374151; border:1px solid #d1d5db;"
    " border-radius:8px; padding:6px 14px; font-size:13px; }"
    "QPushButton:hover { border-color:#5b8cff; color:#5b8cff; }"
)
_BTN_BLUE = (
    "QPushButton { background:#5b8cff; color:#ffffff; border:none;"
    " border-radius:8px; padding:8px 18px; font-size:13px; font-weight:700; }"
    "QPushButton:disabled { background:#9db4e8; }"
)
# 小方按钮（对话字号 A-/A+）：窄、居中、文字不会被省略成方块
_BTN_TINY = (
    "QPushButton { background:#ffffff; color:#374151; border:1px solid #d1d5db;"
    " border-radius:8px; padding:4px 0; font-size:13px; font-weight:700; }"
    "QPushButton:hover { border-color:#5b8cff; color:#5b8cff; }"
)
