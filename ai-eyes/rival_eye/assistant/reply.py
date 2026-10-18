"""回复闭环 v3：发送后盯着目标 AI 客户端窗口，等回复生成完成并读回真实文本。

链路：PrintWindow 原生分辨率采样（隔窗捕获，不打扰用户操作）→ 完成判定
（只有 ≥SIG_AREA 的"大变化"才重置计时——文字流式输出算大变化；光标闪烁、
小动画、旋转指示器永不重置 → AI 写完即判定，不空等）→ 文本提取三层：
①UIA 无障碍树（**结构锚点**：先定位"本轮提问"那条消息节点，只取它之后的内容）
②Windows 系统 OCR（UIA 失败时对截图做中文识别，同样只取"提问之后"的行）
③两者都失败才把截图交上层兜底显示。

v3 修的坑（10.15 实测：对 DSH/Electron 客户端回读，读回来的竟是另一段对话）：
- 旧版靠"sent 末 24 字找最后一次出现"定位 → AI 回复里一旦复述用户原话，锚点漂进回复内部；
- 旧版锚点找不到时取 lines[-14:]（树尾 14 条兜底）→ 虚拟滚动窗口里等于随机抓一段历史；
- 旧版只按 y 过滤 → Electron 把滚出视口的历史节点（y 为负）留在无障碍树里，全被算作对话区；
- 旧版对抓到的内容不做反向校验 → 界面文案（"暂时无法预览""展开状态"）也能当回复交给上层。

v3 的规矩：**宁可不给，也不给错**。找不到本轮提问锚点就返回 None 继续等，
不再用"树尾兜底"猜一段内容——静默给错比超时更难查。
"""
import re
import threading
import time

from . import winrelay

UI_NOISE = ("复制", "重新生成", "重新回答", "赞", "踩", "分享", "编辑", "删除",
            "停止生成", "继续生成", "发送", "新对话", "更多", "点赞", "点踩",
            "思考中", "已深度思考", "深度思考", "展开", "收起")

# 客户端自带的界面文案（不是 AI 正文）：正则命中即丢弃该行。
# 每次抓错都往这里补一条——这是"读错内容"最便宜的疫苗。
_NOISE_RE = (
    r"^\d{1,2}:\d{2}$",                          # 消息时间戳 12:43
    r"^已(停止|完成|取消|中断|排队)",
    r"^(深度)?(求索|思考|生成|推理)中",
    r"用时\s*\d+\s*秒",
    r"^\d+\s*tok/s$",
    r"缓存命中|token-saver|已节省|压缩\s*\d+\s*次|\d+\s*次请求",
    r"余额|^¥\s*[\d.]+|今日已用",
    r"暂时无法预览|无法预览|该格式文件",
    r"^展开状态$|^已折叠$|^折叠$",
    r"^\d+\s*进行中|进行中\s*·|待处理$|\d+\s*已完成",
    r"^上一轮|^本轮|^上下文|对话消耗|token 用量",
    r"^立即(发送)?$|^移除待发送消息$|^待发送$",
    r"^任务$|^文件$|^变更$|^资源管理$|^轮次导航$|^跳转到第|^加载并跳转到第",
    r"未检测到|加载中|Loading|正在(输入|生成)",
)
_NOISE_RX = tuple(re.compile(p) for p in _NOISE_RE)

# "硬"界面文案：只要整段回复里出现任意一行，就判定这次抓取不可信（继续等）。
# 触发场景实录：旧版曾把 DSH 的文件卡片提示语 + 另一段会话内容当成本轮回复。
_HARD_NOISE_RE = (
    r"暂时无法预览|无法预览|该格式文件",
    r"^展开状态$|^已折叠$",
    r"^\d+\s*tok/s$|token-saver|已节省\s*~",
    r"今日已用\s*¥|^¥\s*[\d.]+$|余额$",
    r"^(深度)?(求索|思考|生成|推理)中(，|$)",
    r"用时\s*\d+\s*秒",
)
_HARD_NOISE_RX = tuple(re.compile(p) for p in _HARD_NOISE_RE)

# 客户端自己的"还在生成/还在跑工具"状态。实测 DSH：工具执行期间输入框上方依然写着
# "深度求索中，用时 1分33秒 ···"，旁边还有"正在运行命令"；空闲时才消失或变成"已停止"。
_GEN_RE = re.compile(r"(深度)?(求索|思考|生成|推理|输出)中|正在(生成|思考|输出|运行|执行)|生成中")

# 模块级：最近一次提取为什么没结果（供上层把"目标窗口换了会话"这种情况说清楚）
LAST_REASON = ""


def wait_reply(hwnd, timeout: float = 60.0, idle_sec: float = 2.5, poll: float = 0.35,
               stop=None, sig_area: int = 250, growth_patience: float = 3.0, progress=None):
    """等待回复完成，返回完成时刻的全尺寸截图；超时且从未见大变化返回 None。

    完成判定（满足其一）：
    ① 连续 idle_sec 秒完全无变化（安静客户端）；
    ② 仍有变化，但变化的"联合框"已 growth_patience 秒没有扩大——说明只剩
       原地动画（旋转指示符/思考计时器），文字流式输出已结束。
    文字流式的特征是变化区域不断向下/向右扩展（联合框在长），动画永远原地踏步。
    progress(elapsed_s) 每次采样回调一次（供状态栏倒计时）。
    """
    from PIL import ImageChops

    prev = winrelay.capture_window(hwnd)
    if prev is None:
        return None
    t0 = time.time()
    seen_sig = False
    last_event = None
    union = None
    last_grow = None
    while True:
        el = time.time() - t0
        if stop is not None and stop.is_set():
            return None
        if el >= timeout:
            break
        if progress is not None:
            try:
                progress(int(el))
            except Exception:
                pass
        time.sleep(poll)
        cur = winrelay.capture_window(hwnd)
        if cur is None:  # 窗口暂时最小化/关闭，继续等
            continue
        bbox = ImageChops.difference(prev, cur).getbbox()
        if bbox is not None:
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if w * h >= sig_area:
                now = time.time()
                seen_sig = True
                last_event = now
                if union is None:
                    union, last_grow = bbox, now
                else:
                    uw, uh = union[2] - union[0], union[3] - union[1]
                    nu = (min(union[0], bbox[0]), min(union[1], bbox[1]),
                          max(union[2], bbox[2]), max(union[3], bbox[3]))
                    grew = (nu[2] - nu[0]) > uw + 6 or (nu[3] - nu[1]) > uh + 6
                    if grew or (last_grow is None or now - last_grow > 8.0):
                        last_grow = now
                    union = nu
        if seen_sig:
            now = time.time()
            if last_event is None or now - last_event >= idle_sec:
                break  # 完全静默
            if last_grow is not None and now - last_grow >= growth_patience:
                break  # 只剩原地动画
        prev = cur
    return winrelay.capture_window(hwnd) if seen_sig else None


def _norm(s: str) -> str:
    """匹配用归一化：去掉全部空白（OCR 常在汉字间插空格）。"""
    return "".join(s.split())


def _tidy(s: str) -> str:
    """展示用清理：去掉 OCR 插在汉字之间的空格。"""
    cjk = "\u4e00-\u9fff"
    s = re.sub(rf"(?<=[{cjk}]) +(?=[{cjk}])", "", s)
    return s.strip()


def _is_noise(line: str) -> bool:
    """这一行是不是客户端界面文案（不是 AI 正文）。"""
    raw = (line or "").strip()
    n = _norm(raw)
    if not n or len(n) <= 1:
        return True
    if n in UI_NOISE:
        return True
    for rx in _NOISE_RX:
        try:
            if rx.search(raw):
                return True
        except Exception:
            continue
    return False


def _fp(ct: str, name: str):
    """节点指纹：只认(控件类型, 归一化文本)。**不含坐标**——窗口一滚动坐标就变，
    若把坐标算进指纹，同样的历史内容会显得"新出现"，基线去重就废了。"""
    return (ct or "?", _norm(name))


# ---------------------------------------------------------------- 无障碍树遍历
_TEXTY = ("TextControl", "DocumentControl", "EditControl")
_BLOCKY = ("ListItemControl", "DataItemControl")   # 自带整条文本的块级角色（<li>/表格行）


def _walk_texty(hwnd, max_nodes: int = 20000, budget: float = 15.0):
    """按**文档序**遍历窗口，返回 (rows, parent_of, info)。

    rows 里每一项 = (节点号, 父节点号, 控件类型, 文本, rect|None, 控件对象)；
    收集三类节点：文本类（Text/Document/Edit）与块级角色（ListItem/DataItem，它们
    的 Name 往往就是整条列表项的完整文本，比它的碎片子节点好用得多）。
    info: 节点号 → (控件类型, 文本)，用来判断某个文本节点的**父节点是不是行内样式容器**
    （客户端把 <strong>/<code>/<a> 渲染成一个无名 Text 容器，据此能把加粗/代码还原）。

    文档序很关键：聊天窗口里"提问之后"的内容就是本轮回复，早于提问的都是历史——
    不需要靠坐标猜。带父节点号是为了后面能把"列表项内部的碎片"整段跳过。
    """
    rows, parent_of, info = [], {}, {}
    try:
        import uiautomation as ua
    except ImportError:
        return rows, parent_of, info
    try:
        root = ua.ControlFromHandle(hwnd)
    except Exception:
        return rows, parent_of, info
    if root is None:
        return rows, parent_of, info
    stack = [(root, -1)]
    n = 0
    t0 = time.time()
    while stack and n < max_nodes and time.time() - t0 < budget:
        c, pid = stack.pop()
        my = n
        n += 1
        parent_of[my] = pid
        try:
            ctn = c.ControlTypeName
        except Exception:
            ctn = "?"
        try:
            nm = (c.Name or "").strip()
        except Exception:
            nm = ""
        info[my] = (ctn, nm)
        if ctn in _TEXTY or ctn in _BLOCKY:
            try:
                if len(nm) > 1:
                    r = c.BoundingRectangle
                    rect = (r.left, r.top, r.right, r.bottom) if r else None
                    rows.append((my, pid, ctn, nm, rect, c))
            except Exception:
                pass
        try:
            kids = c.GetChildren()
            if kids:
                stack.extend((k, my) for k in reversed(kids))
        except Exception:
            pass
    return rows, parent_of, info


def _is_descendant(nid, ancestor_id, parent_of) -> bool:
    """nid 是不是 ancestor_id 的后代（用来把列表项内部的碎片整段跳过）。"""
    seen = 0
    cur = parent_of.get(nid)
    while cur is not None and cur >= 0 and seen < 64:
        if cur == ancestor_id:
            return True
        cur = parent_of.get(cur)
        seen += 1
    return False


def _content_box(ctrl, wb):
    """沿锚点往上找一个"正文列"容器（够宽、够高的最内层祖先），用来横切掉侧栏/面板。

    实测（DSH）：正文列 x∈[-1640,-2]，侧栏 x∈[-1908,-1652]——只要按正文列的横向范围
    过滤，左侧会话列表/文件树就不会再混进回复，哪怕基线快照是空的。
    """
    if ctrl is None or wb is None:
        return None
    best = None
    p = ctrl
    span_w = max(1, wb.right - wb.left)
    span_h = max(1, wb.bottom - wb.top)
    for _ in range(12):
        try:
            p = p.GetParentControl()
        except Exception:
            break
        if p is None:
            break
        try:
            r = p.BoundingRectangle
        except Exception:
            continue
        if not r:
            continue
        if (r.right - r.left) >= span_w * 0.35 and (r.bottom - r.top) >= span_h * 0.4:
            best = (r.left, r.top, r.right, r.bottom)
    return best



def _bottom_limit(hwnd):
    """对话区下界（屏幕 y）：再往下就是输入框/状态栏，不是 AI 正文。

    只对**输入框上沿 8px 以上**设硬边界——客户端常在输入框上方放"思考中/任务/token"
    等提示条，那些交给 _is_noise 逐行否掉；早先的"输入框上沿-150px"一刀切会把
    紧贴输入框的最新消息（也就是本轮提问本身）也切掉，锚点就永远找不到了。
    """
    try:
        wb = winrelay.window_bounds(hwnd)
    except Exception:
        return None
    in_rect = winrelay._uia_chat_input_rect(hwnd)
    if in_rect:
        return in_rect[1] - 8
    return wb.bottom - max(160, (wb.bottom - wb.top) // 6)


def _find_anchor(rows, sent_text: str, bottom_y, baseline=None):
    """找"本轮提问"那条消息节点在 rows 里的下标。

    强匹配：节点文本以提问开头（聊天窗里用户消息就是原文本）；
    短消息（"测试""111"这种≤3 字）：文本太短没法只靠前缀认，额外要求该节点是
    **发送前不存在的新节点**（基线里没有），否则宁可不用；
    弱匹配：节点文本包含提问的末 24 字（个别客户端会加前缀）。
    取**文档序最后一个**命中——同一句话发两次时，永远锚定最新那次；
    一个都没有 → None（_uia_texts 会退回"基线位置锚点"，或干脆不给）。
    """
    key = _norm(sent_text or "")
    if not key:
        return None
    fps, txts = _base_sets(baseline)
    head = key[:16]
    strong, exact, weak, fuzzy = [], [], [], []
    for i, (_nid, _pid, ct, nm, rect, _c) in enumerate(rows):
        if bottom_y and rect and (rect[1] + rect[3]) / 2 >= bottom_y:
            continue
        n = _norm(nm)
        if len(key) >= 4 and n.startswith(head):
            strong.append(i)
        elif n == key:
            # 完全相等：短消息（"测试""111"）主要靠这条；也覆盖"客户端把用户消息
            # 单独渲染成一个节点"的情况。不要求"发送前不存在"——同一句话发第二遍时
            # 指纹跟旧的一模一样，用基线去重会把新那条一起排除（10.15 实测踩过）。
            exact.append(i)
        elif len(key) >= 8 and key[-24:] in n:
            weak.append(i)
        elif len(key) >= 6 and len(n) <= len(key) + 40 and key[-8:] in n:
            # 兜底：客户端把一条消息拆成多个文本节点（例如输入框里原有半句草稿，
            # 我们粘贴的后半句被单独渲染），此时哪个节点都不"以提问开头"，
            # 但尾部这 8 个字一定在。限定节点长度，避免把回复里引用的话当锚点。
            fuzzy.append(i)
    for bucket in (strong, exact, weak, fuzzy):
        if bucket:
            return bucket[-1]
    return None


def _base_sets(baseline):
    """把基线整理成 {(类型, 归一化文本)} 与 {归一化文本} 两个集合。"""
    fps, txts = set(), set()
    for f in (baseline or ()):
        try:
            if isinstance(f, (tuple, list)) and len(f) == 2:
                fps.add((f[0], f[1]))
                txts.add(f[1])
            else:
                txts.add(_norm(str(f)))
        except Exception:
            continue
    return fps, txts


# ------------------------------------------------------- 版式还原（碎片 → 段落）
_MARKER_RE = re.compile(r"^(?:[•·▪◦*\-]|[（(]?\d{1,3}[.、)）]|[①-⑳])$")


def _join_frag(a: str, b: str) -> str:
    """同一行/软换行的两段碎片直接相接。只有接缝两侧都是 ASCII 字母数字时才补空格
    （英文单词被 <b>/<code> 拆开的情况），中文之间绝不插空格。"""
    if not a:
        return b
    if not b:
        return a
    x, y = a[-1], b[0]
    if x.isascii() and y.isascii() and x.isalnum() and y.isalnum():
        return a + " " + b
    return a + b


def _rect_h(r):
    return (r[3] - r[1]) if r else 0


def _same_line(a, b) -> bool:
    """两个碎片是不是同一视觉行（竖直方向明显重叠）。"""
    if not a or not b:
        return False
    ov = min(a[3], b[3]) - max(a[1], b[1])
    if ov <= 0:
        return False
    return ov >= min(_rect_h(a), _rect_h(b)) * 0.5


# 行内样式还原：Chromium 系客户端把 <strong>/<code>/<a> 渲染成一个**无名 Text 容器**，
# 里面的文字节点就是那个行内片段。字体属性读不到（TextPattern 的 FontName/FontSize 一律
# 返回 NotSupported），但"父节点是无名 Text"这个结构特征很稳——据此把加粗/代码加回记号，
# 控制台再渲染一次，观感就和客户端接近了。
_CODEISH_RE = re.compile(r"^[\w./\\:(){}\[\]<>+\-*=|@#$%^&!?~]{1,60}$")


def _decorate_inline(txt: str) -> str:
    s = (txt or "").strip()
    if not s or len(s) > 60:
        return txt
    if any("\u4e00" <= ch <= "\u9fff" for ch in s):
        return "**%s**" % s               # 含中文的行内片段几乎都是加粗
    if _CODEISH_RE.match(s):
        return "`%s`" % s                 # 无空格 + 标识符/路径形状 → 当行内代码
    return "**%s**" % s


def _soft_wrap(prev, cur, right_edge) -> bool:
    """cur 是不是 prev 那一行的**软换行**继续（而不是新段落）。

    判据：上一行写满了正文列（右边缘顶到列右界），且行距只有一个行高左右。
    没写满的行后面必然是新块——这条比单纯看间距稳得多。
    """
    if not prev or not cur:
        return False
    if _same_line(prev, cur):
        return True
    gap = cur[1] - prev[3]
    if gap < 0:
        return True
    pitch = max(_rect_h(prev), _rect_h(cur), 12)
    if right_edge is not None and prev[2] < right_edge - 30:
        return False
    return gap <= pitch * 1.2


def _assemble(items, right_edge) -> str:
    """把无障碍树的碎片拼回"像 Markdown 的正文"。

    items：[(kind, 文本, rect)]，按文档序，kind ∈ {'text','li'}。
    - 列表项（<li> 在树里是 ListItemControl，Name 就是整条文本）→ "- " / "1. " 前缀，
      不再被它的碎片子节点切成半句；
    - 普通文本 → 同一视觉行直接相接、软换行直接相接、真正的新段落用空行分隔。
    这样控制台再做一次 Markdown 渲染，版式就和客户端看到的差不多了。
    """
    blocks = []           # {'kind','text','last','prefix'}
    pending_marker = None  # 列表项前面那个独立的圆点/序号碎片
    for kind, nm, rect in items:
        if kind == "li":
            body = "".join((nm or "").split("\n")).strip()
            prefix = "- "
            if pending_marker:
                m = pending_marker.strip()
                if m and m not in ("•", "·", "▪", "◦", "*", "-"):
                    prefix = m if m.endswith((".", "、", ")", "）")) else m + "."
                    prefix += " "
                pending_marker = None
            blocks.append({"kind": "li", "text": prefix + body, "last": rect})
            continue
        txt = (nm or "").strip()
        if not txt:
            continue
        if _MARKER_RE.match(_norm(txt)) and len(_norm(txt)) <= 4:
            pending_marker = txt          # 圆点/序号单独成节点，留给下一个列表项
            continue
        if blocks and blocks[-1]["kind"] == "text" and _soft_wrap(blocks[-1]["last"], rect, right_edge):
            blocks[-1]["text"] = _join_frag(blocks[-1]["text"], txt)
            blocks[-1]["last"] = rect
        else:
            blocks.append({"kind": "text", "text": txt, "last": rect})

    out = []
    for i, b in enumerate(blocks):
        if i == 0:
            out.append(b["text"])
        elif b["kind"] == "li" and blocks[i - 1]["kind"] == "li":
            out.append("\n" + b["text"])          # 列表项之间单换行，Markdown 才认成一整列
        else:
            out.append("\n\n" + b["text"])
    return "".join(out)


def _looks_like_user_bubble(ctrl) -> bool:
    """这个节点是不是"消息气泡"容器里的？（客户端一般叫 bubble / message-user 之类）

    用途：拿到锚点之后的正文时，一旦又碰到一个**新的用户气泡**就收手，避免把用户
    接着发的那条话并进回复里（长期已知局限）。只认 class 名，读不到就当 False。
    """
    if ctrl is None:
        return False
    try:
        p = ctrl
        for _ in range(3):
            p = p.GetParentControl()
            if p is None:
                return False
            if "bubble" in (p.ClassName or "").lower():
                return True
    except Exception:
        pass
    return False


def _collect_after(rows, parent_of, idx, bottom_y, baseline, max_chars: int, wb=None, info=None):
    """取锚点之后的内容并还原版式，返回 (文本, 诊断信息)。

    过滤链：
    ① 几何：正文列以外的（侧栏/文件树）、输入框/状态栏以下的都不要；
    ② 基线：发送前就存在的文本节点（跨会话残留、虚拟列表历史）不要；
    ③ 文案：客户端界面文案、碎数字不要；
    ④ 结构：列表项用它的整条 Name，内部的碎片子节点整段跳过；
       父节点是无名 Text 的片段 = 加粗/行内代码，加回 Markdown 记号。

    刻意**不**按"右对齐小气泡"去截断：那种几何判断在这个客户端上会被侧栏/状态条
    污染，一旦误判就会把好回复截半句——静默截断比多读一句更糟。用户在自己等待期间
    又发消息会被并进正文，这是已知局限（待办：按消息角色容器区分）。
    """
    fps, txts = _base_sets(baseline)
    info = info or {}
    anchor_rect = rows[idx][4]
    anchor_top = anchor_rect[1] if anchor_rect else None
    box = _content_box(rows[idx][5], wb) if wb is not None else None

    seg, blocked_depth, dropped_box, styled, stopped_bubble = [], 0, 0, 0, 0
    used_block_ids = []
    for r in rows[idx + 1:]:
        nid, pid, ct, nm, rect, _c = r
        if bottom_y and rect and (rect[1] + rect[3]) / 2 >= bottom_y:
            continue  # 输入框/状态栏以下
        if box and rect and not (rect[2] > box[0] + 4 and rect[0] < box[2] - 4):
            dropped_box += 1
            continue  # 正文列以外（左侧会话列表、右侧轮次导航…）
        # 又碰到一条新的用户气泡 = 本轮回复到此为止（用户等待期间又发了话）
        if seg and _looks_like_user_bubble(_c):
            if not (rect and anchor_rect and rect[0] < anchor_rect[0] - 80):
                stopped_bubble += 1
                break
        # 聊天窗里"提问之后的正文"在竖直方向一定不高于提问本身；明显跑到提问上方去的，
        # 只会是另一段会话残留在文档序里插进来的历史（基线缺失时的第二道闸）。
        if anchor_top is not None and rect and rect[3] < anchor_top - 60:
            continue
        # 已经当作列表项整条发出去的块，跳过它内部的碎片
        if any(_is_descendant(nid, bid, parent_of) for bid in used_block_ids):
            blocked_depth += 1
            continue
        n = _norm(nm)
        if len(n) >= 6 and ((ct, n) in fps or n in txts):
            continue  # 旧内容
        if ct in _BLOCKY:
            if n in ("", "-"):
                continue
            seg.append(("li", nm, rect))
            used_block_ids.append(nid)
            continue
        if _is_noise(nm):
            continue
        if _MARKER_RE.match(n) and len(n) <= 4:
            seg.append(("text", nm, rect))   # 圆点/序号标记，交给 _assemble 做前缀
            continue
        # 只丢"纯数字/符号"的碎片（状态栏计数之类，如 "47"）；短的中文碎片要留——
        # 客户端会把 <strong>之前</strong> 这种加粗词切成独立节点，丢了就会少字。
        if len(n) < 4 and re.fullmatch(r"[\d\s.,:%¥+\-()（）\[\]|/]+", n):
            continue
        pct, pnm = info.get(pid, ("", ""))
        if pct == "TextControl" and not pnm and 1 < len(n) <= 60:
            nm = _decorate_inline(nm)        # 行内样式片段：加回 **加粗** / `代码`
            styled += 1
        seg.append(("text", nm, rect))

    # 正文列右界：用来判断"上一行写满了没有"（软换行 vs 新段落）
    rights = [r[2] for k, _nm, r in seg if r]
    right_edge = max(rights) if rights else None
    reply = _tidy(_assemble(seg, right_edge))
    info_out = {"rows": len(rows), "candidates": len(seg), "kept": len(seg),
                "dropped_box": dropped_box, "blocked": blocked_depth, "styled": styled,
                "stopped_bubble": stopped_bubble}
    return (reply[:max_chars] if reply.strip() else None), info_out


def _uia_texts(hwnd, sent_text: str, max_chars: int = 1500, max_nodes: int = 20000,
               baseline=None, log: bool = False):
    """UIA 提取本轮回复正文。找不到锚点就返回 None（不猜）。"""
    try:
        rows, parent_of, info = _walk_texty(hwnd, max_nodes, 10.0)
        if not rows:
            # Chromium 系（浏览器/Electron）的无障碍树是懒加载：首次查询触发构建但返回空，
            # 等 1.5 秒再查一次就有了。首次查询本身就是"预热"。
            # 两次遍历合计要留在 extract_reply_text 的 25 秒 join 之内（10+1.5+10）。
            time.sleep(1.5)
            rows, parent_of, info = _walk_texty(hwnd, max_nodes, 10.0)
        if not rows:
            return None
        bottom_y = _bottom_limit(hwnd)
        try:
            wb = winrelay.window_bounds(hwnd)
        except Exception:
            wb = None
        idx = _find_anchor(rows, sent_text, bottom_y, baseline)
        if idx is None and not _norm(sent_text or ""):
            # 只发截图：用"基线里最后一条旧内容"当位置锚点
            fps, txts = _base_sets(baseline)
            for i, (_nid, _pid, ct, nm, rect, _c) in enumerate(rows):
                if bottom_y and rect and (rect[1] + rect[3]) / 2 >= bottom_y:
                    continue
                if (ct, _norm(nm)) in fps or _norm(nm) in txts:
                    idx = i
        if idx is None:
            global LAST_REASON
            LAST_REASON = "noanchor"
            if log:
                _reply_log("提取：未找到本轮提问锚点，本轮不猜（等窗口更新）｜节点 %d" % len(rows))
            return None
        text, info_out = _collect_after(rows, parent_of, idx, bottom_y, baseline, max_chars, wb, info)
        LAST_REASON = "" if text else "empty"
        if log:
            _reply_log("提取：节点 %d｜锚点 #%d｜候选 %d｜列外丢弃 %d｜块内碎片 %d｜行内样式 %d%s｜得 %d 字"
                       % (info_out["rows"], idx, info_out["candidates"], info_out["dropped_box"],
                          info_out["blocked"], info_out["styled"],
                          "｜遇到新用户气泡收手" if info_out["stopped_bubble"] else "",
                          len(_norm(text or ""))))
        return text
    except Exception:
        return None


def extract_reply_text(hwnd, sent_text: str, max_chars: int = 1500,
                       max_nodes: int = 20000, baseline=None, log: bool = False) -> str | None:
    """UIA 读取窗口文本（独立线程执行，规避 COM 公寓冲突；25 秒硬超时）。
    baseline：发送前的文本节点指纹快照，用来把旧内容（含跨会话残留）剔出去。"""
    box = [None]

    def _run():
        box[0] = _uia_texts(hwnd, sent_text, max_chars, max_nodes, baseline, log)

    t = threading.Thread(target=_run, daemon=True, name="uia-extract")
    t.start()
    t.join(timeout=25)
    return box[0]


def uia_snapshot(hwnd) -> list:
    """发送前快照：窗口当前全部文本节点的指纹 (ControlType, 归一化文本)。

    注意存的是指纹不是文本清单：提取时要做"这条内容在发送前就存在吗"的判断，
    而窗口滚动/重排会让坐标变、虚拟列表会复用节点，只有指纹是稳定的。
    空结果会重查一次——Electron 的树首次查询常常是空的。
    """
    box = [[]]

    def _run():
        try:
            rows, _parent, _info = _walk_texty(hwnd, 20000, 12.0)
            if not rows:
                time.sleep(1.5)
                rows, _parent, _info = _walk_texty(hwnd, 20000, 12.0)
            box[0] = [_fp(ct, nm) for _nid, _pid, ct, nm, _rect, _c in rows]
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True, name="uia-snapshot")
    t.start()
    t.join(timeout=15)
    return box[0]


def warm_up_uia(hwnd) -> None:
    """后台触发目标窗口构建无障碍树（Chromium 懒加载），后续回复提取立即可用。"""
    try:
        t = threading.Thread(target=lambda: _walk_texty(hwnd, 2500, 10.0),
                             daemon=True, name="uia-warmup")
        t.start()
    except Exception:
        pass


def ocr_image(img):
    """Windows 系统 OCR，语言回退 zh-Hans-CN → zh → en。不可用/失败返回 None。

    winocr 新版把 WinRT 结果 picklify 成 dict（{"text": ...}），旧版是带 .text 的对象，两者都兼容。
    """
    try:
        import winocr
    except ImportError:
        return None
    for lang in ("zh-Hans-CN", "zh", "en"):
        try:
            res = winocr.recognize_pil_sync(img, lang)
            if isinstance(res, dict):
                text = res.get("text") or ""
            else:
                text = getattr(res, "text", "") or ""
            if text.strip():
                return text
        except Exception:
            continue
    return None


def _pick_reply(lines, sent_text: str, max_chars: int = 1500) -> str | None:
    """从文本行序列里取"我方提问之后"的回复（UIA 节点与 OCR 行共用）。

    锚点找不到 → None。旧版这里会退化成"取最后 14 行"，在聊天窗口里那等于
    随机抓一段历史（10.15 实战抓错过一整段别的对话），已删除。
    """
    lines = [l.strip() for l in lines if l and l.strip()]
    if not lines:
        return None
    key = _norm(sent_text)[-24:] if sent_text and sent_text.strip() else ""
    if not key:
        return None
    idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if key in _norm(lines[i]):
            idx = i
            break
    if idx < 0:
        return None
    parts = [l for l in lines[idx + 1:] if not _is_noise(l)]
    reply = _tidy("\n".join(parts))
    return reply[:max_chars] if reply.strip() else None


def ocr_reply_text(img, sent_text: str, max_chars: int = 1500) -> str | None:
    from PIL import Image

    # Windows OCR 对小字号敏感：2 倍 LANCZOS 放大后识别率显著提升（3 倍反而过平滑）
    big = img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
    lines = (ocr_image(big) or "").splitlines()
    return _pick_reply(lines, sent_text, max_chars)


def _valid_reply(text: str | None) -> bool:
    """抓回来的东西像不像"一段 AI 正文"（而不是排队回显/界面文案）。

    不达标就让上层继续等——旧版只看字数，抓到 41 字的"展开状态"也当成功，
    于是监听提前收工、真回复永远丢失。
    """
    if not text:
        return False
    n = _norm(text)
    if len(n) < 24:
        return False
    lines = [l for l in text.splitlines() if _norm(l)]
    if not lines:
        return False
    if all(_is_noise(l) for l in lines):
        return False
    # 硬界面文案：混进来一行就说明这次抓的不是干净的正文
    for l in lines:
        for rx in _HARD_NOISE_RX:
            if rx.search(l.strip()):
                return False
    return True


def _reply_log(msg: str) -> None:
    """回复链路逐步留痕（RivalEye-回复日志.txt），排查"没收到回复"。"""
    import datetime
    import os
    import sys

    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        path = os.path.join(base, "RivalEye-回复日志.txt")
        text = open(path, encoding="utf-8", errors="replace").read() if os.path.exists(path) else ""
        lines = (text.splitlines() + [f"[{datetime.datetime.now():%H:%M:%S}] {msg}"])[-60:]
        open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    except Exception:
        pass


def _generating_now(hwnd) -> bool:
    """客户端自己说"我还在生成/还在跑工具"吗？只在准备收工的那一刻调用（限时遍历）。

    判据取窄：只看**输入框上方那条状态区**里的短节点，并且跳过待办摘要
    （"1 进行中 · 4 待处理"那种是清单，不是生成状态）。取不到提示就返回 False，
    行为跟以前一致；一旦读错（把状态区认没了）也只是白等，不会读错内容。
    """
    try:
        rows, _parent, _info = _walk_texty(hwnd, 4000, 3.0)
    except Exception:
        return False
    try:
        in_rect = winrelay._uia_chat_input_rect(hwnd)
    except Exception:
        in_rect = None
    top = in_rect[1] if in_rect else None
    for _nid, _pid, _ct, nm, rect, _c in rows:
        s = (nm or "").strip()
        if not s or len(_norm(s)) > 40:
            continue
        if top is not None and rect and not (top - 190 < rect[1] < top + 40):
            continue                                    # 只看输入框附近的状态区
        if re.match(r"^\d+\s*进行中|进行中\s*·", s):
            continue                                    # 待办摘要，不是生成状态
        if _GEN_RE.search(s):
            return True
    return False


def stream_reply(hwnd, sent_text: str, baseline=None, stop=None, timeout: float = 600.0,
                 idle_sec: float = 3.0, poll: float = 0.5, progress=None, partial_cb=None):
    """流式回复闭环：盯目标窗口直到生成结束，期间每 ~3 秒回调一次已生成的部分文本
    （partial_cb），AI 长思考/工具调用期间不会误判提前结束。返回 (text|None, img|None, mode)。"""
    from PIL import ImageChops

    _reply_log(f"开始监听「{winrelay.window_title(hwnd)[:24]}」 sent={sent_text[:16]!r}")
    # 首选：DSH 会话文件直读 —— 拿到的是原始 Markdown，且**用户在等待期间切到别的会话
    # 也照样读得到**（屏幕不渲染这条消息也不影响）。读不到就退回下面的屏幕识别。
    try:
        from . import dshstore

        if dshstore.available() and dshstore.looks_like_target(hwnd):
            _reply_log("走 DSH 会话直读…")
            txt, spath = dshstore.reply_after(sent_text, title=winrelay.window_title(hwnd),
                                              timeout=timeout, stop=stop, progress=progress,
                                              on_partial=partial_cb, poll=0.6)
            if txt:
                _reply_log("完成：会话直读 %d 字｜头:%r｜尾:%r" % (len(txt), txt[:28], txt[-28:]))
                return txt, None, "store"
            _reply_log("会话直读没拿到正文，退回屏幕识别")
    except Exception as e:
        _reply_log("会话直读异常，退回屏幕识别：%r" % (e,))
    prev = winrelay.capture_window(hwnd, 640)
    if prev is None:
        _reply_log("失败：无法捕获目标窗口（最小化或已关闭？）")
        return None, None, "timeout"
    t0 = time.time()
    seen_sig = False
    last_big = None
    union = None
    last_grow = None
    last_partial_at = 0.0
    last_partial = ""
    while True:
        el = time.time() - t0
        if stop is not None and stop.is_set():
            _reply_log("取消：用户再次发送")
            return None, None, "cancelled"
        if el >= timeout:
            break
        if progress is not None:
            try:
                progress(int(el))
            except Exception:
                pass
        time.sleep(poll)
        cur = winrelay.capture_window(hwnd, 640)
        if cur is None:
            continue
        bbox = ImageChops.difference(prev, cur).getbbox()
        if bbox is not None:
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if bw * bh >= 250:
                now = time.time()
                seen_sig = True
                last_big = now
                if union is None:
                    union, last_grow = bbox, now
                else:
                    uw, uh = union[2] - union[0], union[3] - union[1]
                    nu = (min(union[0], bbox[0]), min(union[1], bbox[1]),
                          max(union[2], bbox[2]), max(union[3], bbox[3]))
                    grew = (nu[2] - nu[0]) > uw + 6 or (nu[3] - nu[1]) > uh + 6
                    if grew or (last_grow is None or now - last_grow > 10.0):
                        last_grow = now
                    union = nu
        # 流式预览：每 3 秒提取一次当前增量文本
        if partial_cb is not None and seen_sig and time.time() - last_partial_at >= 3.0:
            last_partial_at = time.time()
            try:
                p = extract_reply_text(hwnd, sent_text, max_chars=600, max_nodes=6000,
                                       baseline=baseline)
                if p and p != last_partial:
                    last_partial = p
                    partial_cb(p)
            except Exception:
                pass
        if seen_sig:
            now = time.time()
            due = ((last_big is None or now - last_big >= idle_sec)
                   or (last_grow is not None and now - last_grow >= 10.0))
            if due:
                # 准备收工前先问一句客户端：你还在生成吗？还在就把计时按回去继续等
                # （工具调用/长思考的间隙，屏幕会安静十几秒甚至几分钟）
                if _generating_now(hwnd):
                    last_big = now
                    last_grow = now
                else:
                    break  # 静默或只剩原地动画，且客户端也说写完了
        prev = cur
    img = winrelay.capture_window(hwnd) if seen_sig else None
    if img is None:
        _reply_log(f"超时({timeout:.0f}s)且未见内容变化")
        return None, None, "timeout"
    # 生成结束 ≠ 正文写完：要求**连续两次读数一致**才收工（渲染尾巴、分批上屏都在这段兜住）。
    # 有效内容（≥24 字、无界面文案）稳定 2 轮即交；短内容稳定 4 轮也如实交，不陪它耗到超时。
    text = extract_reply_text(hwnd, sent_text, baseline=baseline, log=True)
    settle, rounds = 0, 0
    title0 = ""
    try:
        title0 = winrelay.window_title(hwnd)
    except Exception:
        pass
    while time.time() - t0 < timeout - 5:
        if stop is not None and stop.is_set():
            _reply_log("取消：用户再次发送（等待有效回复中）")
            return None, None, "cancelled"
        # 目标窗口的标题就是"当前显示的会话名"：换了会话，本轮提问节点就不在文档里了。
        # 早一点说清楚，用户切回去还能补上（监听不退出，继续等）。
        try:
            if title0 and winrelay.window_title(hwnd) != title0:
                global LAST_REASON
                LAST_REASON = "switched"
        except Exception:
            pass
        time.sleep(3.0)
        rounds += 1
        again = extract_reply_text(hwnd, sent_text, baseline=baseline, log=(rounds % 5 == 1))
        if again and _norm(again) != _norm(text or ""):
            if not text or len(again) >= len(text):
                text = again
            settle = 0
            continue
        settle += 1
        if _valid_reply(text) and settle >= 2:
            break
        if text and _norm(text) and settle >= 4:
            _reply_log("完成：内容偏短但已稳定（连续 %d 轮不变），如实交出" % settle)
            break
    if text:
        # 渲染可能有尾巴：静置 2 秒再读一次，取更长的（时机更稳、内容更完整）
        time.sleep(2.0)
        try:
            again = extract_reply_text(hwnd, sent_text, baseline=baseline)
            if again and len(again) > len(text):
                text = again
        except Exception:
            pass
        _reply_log(f"完成：UIA 提取 {len(text)} 字｜头:{text[:28]!r}｜尾:{text[-28:]!r}")
        return text, img, "uia"
    ocr = ocr_reply_text(img, sent_text)
    if ocr:
        _reply_log(f"完成：OCR 提取 {len(ocr)} 字")
        return ocr, img, "ocr"
    _reply_log("完成：无法提取文本，回填截图（原因：%s）" % (LAST_REASON or "unknown"))
    return None, img, ("noanchor" if LAST_REASON == "noanchor" else "none")
