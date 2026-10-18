"""DSH（DeepSeek Harness）会话直读：直接从客户端落盘的会话文件里取回复正文。

为什么单独走这条路（而不是继续盯屏幕）——DSH 把每个会话写进
    %USERPROFILE%\\.dsh\\sessions\\<工作区>\\<session-id>\\session.v4.jsonl.zstd
（zstd 压缩的 JSONL，事件流：user/message、assistant/message、tool/call、turn/end …）。

从文件里读的好处，正好是屏幕识别最脆的几处：
  · 拿到的是**原始 Markdown**（加粗/行内代码/列表/表格全在），不用从无障碍树里猜样式；
  · 用户在等待期间**切到别的会话**也照样读得到（屏幕没渲染这条消息也不影响）；
  · 不依赖焦点、按键时序、虚拟滚动、无障碍树懒加载。

读不到（别的客户端 / DSH 改了格式 / 没装 zstandard）就返回 None，调用方自动退回屏幕识别。
"""
import glob
import json
import os
import time


def available() -> bool:
    """能不能走这条路：装了 zstandard，且有会话目录。"""
    try:
        import zstandard  # noqa: F401
    except Exception:
        return False
    return bool(_session_files())


def looks_like_target(hwnd) -> bool:
    """目标窗口是不是 DSH（窗口标题形如 "会话名 — DeepSeek Harness"）。"""
    try:
        from . import winrelay

        return "DeepSeek Harness" in winrelay.window_title(hwnd)
    except Exception:
        return False


def _session_files(limit: int = 40):
    base = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")
    try:
        files = glob.glob(os.path.join(base, "**", "session.v4.jsonl.zstd"), recursive=True)
    except Exception:
        return []
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files[:limit]


_CACHE = {}      # path -> ((mtime_ns, size), events)：文件没变就不重复解压（8MB zstd 解压要 ~0.5s）


def _load(path):
    """读一个会话文件 → 事件列表（失败返回 None）。按 (mtime, size) 缓存，轮询时几乎零开销。"""
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except Exception:
        return None
    hit = _CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        import zstandard as zstd

        with open(path, "rb") as f:
            raw = zstd.ZstdDecompressor().stream_reader(f).read()
    except Exception:
        return None
    events = []
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line.decode("utf-8", "replace")))
        except Exception:
            continue
    if len(_CACHE) > 60:
        _CACHE.clear()
    _CACHE[path] = (key, events)
    return events


def _norm(s: str) -> str:
    return "".join((s or "").split())


def _content_text(content) -> str:
    """把消息的 content 里**可见文本**块拼起来（跳过 reasoning 思考块）。"""
    if isinstance(content, str):
        return content.strip()
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and b.get("text"):
            parts.append(str(b["text"]).strip())
    return "\n\n".join(p for p in parts if p).strip()


def _user_text(ev) -> str:
    return _content_text((ev.get("data") or {}).get("content"))


def _assistant_text(ev) -> str:
    return _content_text(((ev.get("data") or {}).get("message") or {}).get("content"))


def _title_of(events) -> str:
    for ev in reversed(events):
        if ev.get("type") == "session/title":
            t = (ev.get("data") or {}).get("title")
            if t:
                return str(t)
    return ""


def find_session(text: str, title: str = ""):
    """找包含这条用户消息的会话文件；返回 (path, events)。

    优先按标题匹配（窗口标题就是当前会话名，例如 "黑客松 — DeepSeek Harness"），
    再看哪条 user/message 里含这段文字（取最后一条命中的 —— 补读旧消息也用它）。
    """
    key = _norm(text)
    if len(key) < 2:
        return None, None
    tail = key[-24:]
    want_title = (title or "").split("—")[0].strip()
    fallback = None
    for path in _session_files():
        events = _load(path)
        if not events:
            continue
        hit = False
        for e in events:
            if e.get("type") == "user/message" and tail in _norm(_user_text(e)):
                hit = True
                break
        if not hit:
            continue
        if want_title and _title_of(events) == want_title:
            return path, events
        if fallback is None:
            fallback = (path, events)
    return fallback if fallback else (None, None)


def reply_after(text: str, title: str = "", timeout: float = 600.0, stop=None,
                progress=None, on_partial=None, poll: float = 0.6, settle: int = 2):
    """等这条用户消息之后的助手正文，返回 (markdown 文本 | None, 会话文件路径)。

    只取"这条用户消息之后、下一条用户消息之前"的助手可见文本（含中间解说），与窗口里看到的一致。
    收工判据（满足其一）：
      ① 该区间内出现 turn/end（这一轮明确结束）；
      ② 正文连续 settle 次轮询没有变化。
    """
    t0 = time.time()
    path, events = find_session(text, title)
    if not events:
        return None, ""
    key = _norm(text)
    user_seq, next_user_seq = None, None
    for e in events:
        if e.get("type") != "user/message":
            continue
        seq = e.get("seq") or 0
        if key[-24:] in _norm(_user_text(e)):
            user_seq = seq                     # 取最后一条命中的
    for e in events:
        if e.get("type") == "user/message" and (e.get("seq") or 0) > (user_seq or 0):
            next_user_seq = e.get("seq")
            break
    if user_seq is None:
        return None, path
    last_text, stable = "", 0
    while time.time() - t0 < timeout:
        if stop is not None and stop.is_set():
            return None, path
        if progress is not None:
            try:
                progress(int(time.time() - t0))
            except Exception:
                pass
        fresh = _load(path)                     # 会话是追加写的，重读很快
        if not fresh:
            time.sleep(poll)
            continue
        parts, ended = [], False
        for ev in fresh:
            seq = ev.get("seq") or 0
            if seq <= user_seq:
                continue
            if next_user_seq and seq >= next_user_seq:
                break                           # 下一轮提问开始了，本轮到此为止
            if ev.get("type") == "assistant/message":
                t = _assistant_text(ev)
                if t:
                    parts.append(t)
            elif ev.get("type") == "turn/end":
                ended = True
        cur = "\n\n".join(parts).strip()
        if cur and on_partial is not None and cur != last_text:
            try:
                on_partial(cur)
            except Exception:
                pass
        if cur and cur == last_text:
            stable += 1
        else:
            stable = 0
            last_text = cur
        if cur and (ended or stable >= settle):
            return cur, path
        time.sleep(poll)
    return (last_text or None), path
