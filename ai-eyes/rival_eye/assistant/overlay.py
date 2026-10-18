"""悬浮窗 UI（Tkinter，零额外 GUI 依赖）。

交互闭环：启动 → 右下角悬浮球（置顶/半透明/可拖拽）
        → 左键单击弹出面板（回答区 + 输入框）
        → 输入回车/点发送 → 桌面截图 + 问题交给 AssistantAgent → 流式逐字回显
        → 点「收起」回到悬浮球，「退出」结束进程。

线程纪律：工作线程只做截图与网络，一切 UI 更新经 root.after 回主线程。
Windows 上用 -transparentcolor 实现真圆形悬浮球；其他平台退化为方形贴片。
"""
import threading
import tkinter as tk

MAGIC = "#010203"  # 透明色键：Windows 下把画布背景抠成透明


class OverlayApp:
    BALL = 64
    PANEL_W = 380
    PANEL_H = 480
    DRAG_THRESHOLD = 6  # 位移超过该像素视为拖拽而非点击

    def __init__(self, agent):
        self.agent = agent
        self.busy = False
        self.mode = "ball"

        self.root = tk.Tk()
        self.root.title("竞眼雷达助手")
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        self.root.overrideredirect(True)

        try:
            self.root.attributes("-transparentcolor", MAGIC)
            self.canvas_bg = MAGIC
        except tk.TclError:
            self.canvas_bg = "#1a2233"

        self._place_bottom_right(self.BALL, self.BALL)
        self._build_ball()
        self._build_panel()
        self._show_ball()

    # ---------- 悬浮球 ----------
    def _build_ball(self):
        self.ball = tk.Canvas(self.root, width=self.BALL, height=self.BALL,
                              highlightthickness=0, bg=self.canvas_bg, cursor="hand2")
        self.ball.create_oval(4, 4, self.BALL - 4, self.BALL - 4,
                              fill="#2b3a67", outline="#5b8cff", width=2)
        self.ball.create_text(self.BALL // 2, self.BALL // 2 - 9, text="雷达",
                              fill="white", font=("Microsoft YaHei", 12, "bold"))
        self.ball.create_text(self.BALL // 2, self.BALL // 2 + 13, text="AI 助手",
                              fill="#aab3c5", font=("Microsoft YaHei", 8))
        self.ball.bind("<ButtonPress-1>", self._press)
        self.ball.bind("<B1-Motion>", self._drag)
        self.ball.bind("<ButtonRelease-1>", self._release)

    # ---------- 面板 ----------
    def _build_panel(self):
        self.panel = tk.Frame(self.root, bg="#f4f6fb")

        header = tk.Frame(self.panel, bg="#1a2233", height=42)
        header.pack(fill="x")
        header.pack_propagate(False)
        title = tk.Label(header, text="📡 竞眼雷达助手", bg="#1a2233", fg="white",
                         font=("Microsoft YaHei", 11, "bold"))
        title.pack(side="left", padx=10)
        tk.Button(header, text="收起", command=self._show_ball, bd=0,
                  bg="#2b3a67", fg="white", cursor="hand2").pack(side="right", padx=(0, 6), pady=8)
        tk.Button(header, text="退出", command=self.root.destroy, bd=0,
                  bg="#2b3a67", fg="white", cursor="hand2").pack(side="right", pady=8)
        # 按住标题栏可拖动整个面板
        for w in (header, title):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._drag)

        self.output = tk.Text(self.panel, wrap="word", relief="flat", padx=10, pady=8,
                              font=("Microsoft YaHei", 11), bg="white", fg="#1a2233")
        self.output.pack(fill="both", expand=True, padx=8, pady=8)
        self._append(
            "我是竞眼雷达助手 👀\n"
            "发送提问的那一刻，我会截取你的桌面画面来看（只看像素，不读代码），"
            "并结合最新雷达报告回答。\n\n"
            "试试问我：「屏幕上这个页面卖多少钱？」「和我盯的竞品比怎么样？」"
        )

        bottom = tk.Frame(self.panel, bg="#f4f6fb")
        bottom.pack(fill="x", padx=8, pady=(0, 4))
        self.entry = tk.Entry(bottom, font=("Microsoft YaHei", 11), relief="flat", bg="white")
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))
        self.entry.bind("<Return>", lambda _e: self._send())
        self.send_btn = tk.Button(bottom, text="发送", command=self._send, bd=0, width=8,
                                  bg="#5b8cff", fg="white", cursor="hand2",
                                  font=("Microsoft YaHei", 10, "bold"))
        self.send_btn.pack(side="right")

        self.status = tk.Label(self.panel, text="", bg="#f4f6fb", fg="#6b7280",
                               font=("Microsoft YaHei", 9), anchor="w")
        self.status.pack(fill="x", padx=10, pady=(0, 6))

    # ---------- 拖拽 / 展开 ----------
    def _press(self, e):
        self._press_pos = (e.x_root, e.y_root)
        self._win_pos = (self.root.winfo_x(), self.root.winfo_y())
        self._moved = False

    def _drag(self, e):
        dx = e.x_root - self._press_pos[0]
        dy = e.y_root - self._press_pos[1]
        if abs(dx) + abs(dy) > self.DRAG_THRESHOLD:
            self._moved = True
        self.root.geometry(f"+{self._win_pos[0] + dx}+{self._win_pos[1] + dy}")

    def _release(self, _e):
        if not self._moved and self.mode == "ball":
            self._show_panel()

    # ---------- 模式切换 ----------
    def _show_ball(self):
        self.panel.pack_forget()
        self.ball.pack()
        self.mode = "ball"
        self.root.geometry(f"{self.BALL}x{self.BALL}"
                           f"+{self.root.winfo_x()}+{self.root.winfo_y()}")

    def _show_panel(self):
        self.ball.pack_forget()
        self.panel.pack(fill="both", expand=True)
        self.mode = "panel"
        x = max(0, self.root.winfo_x() - self.PANEL_W + self.BALL)
        y = max(0, self.root.winfo_y() - 40)
        self.root.geometry(f"{self.PANEL_W}x{self.PANEL_H}+{x}+{y}")
        self.entry.focus_set()

    def _place_bottom_right(self, w, h):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{sw - w - 40}+{sh - h - 90}")

    # ---------- 提问 / 流式回显 ----------
    def _append(self, text):
        self.output.config(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.config(state="disabled")

    def _send(self):
        if self.busy:
            return
        question = self.entry.get().strip()
        if not question:
            return
        self.busy = True
        self.send_btn.config(state="disabled")
        self._append(f"\n\n🧑 {question}\n🤖 ")
        self.entry.delete(0, "end")
        self.status.config(text="正在截取桌面画面并思考…")
        threading.Thread(target=self._worker, args=(question,), daemon=True).start()

    def _worker(self, question):
        state = {"streamed": False}

        def on_delta(chunk):
            state["streamed"] = True
            self.root.after(0, self._append, chunk)

        try:
            reply = self.agent.ask(question, on_delta=on_delta)
            if not state["streamed"]:
                self.root.after(0, self._append, reply)
            self.root.after(0, self._finish, None)
        except Exception as e:  # 截图失败 / 网络失败都落在面板里，不崩进程
            self.root.after(0, self._finish, str(e))

    def _finish(self, err):
        self.busy = False
        self.send_btn.config(state="normal")
        self._append("\n" if err is None else f"\n[出错] {err}\n")
        self.status.config(text="")

    def run(self):
        self.root.mainloop()
