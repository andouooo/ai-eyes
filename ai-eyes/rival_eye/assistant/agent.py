"""悬浮窗助手 Agent：桌面截图 + 最新雷达报告 + 用户提问 → VLM → 流式回答。

设计红线（竞品缺点改正）：
- Agent 只允许"看"：依据真实屏幕像素与雷达报告作答，禁止渲染页面/查询代码来补全信息；
- 只在用户提问那一刻截屏，不做持续监控（区别于 MineContext 的 5 秒轮询）；
- 无 API Key 自动降级 Mock 模式，现场断网也能演示交互闭环。
"""
from ..config import REPORT_DIR, VLM_API_KEY, VLM_BASE_URL, VLM_MODEL
from .screenshot import grab_screen_jpeg_uri

SYSTEM_PROMPT = """你是「竞眼雷达 RivalEye」的桌面助手。每次用户提问时，你会收到：当前桌面截图、雷达最新报告摘要、用户问题。
回答规则：
1. 只能依据截图里真实可见的内容和雷达报告回答，禁止凭想象编造截图外或代码里的细节；
2. 看不清或不在截图里的内容，明确说"截图里看不到"，并建议用户把相关窗口调到前台再问；
3. 先给一句话结论，再给 1-3 条依据；内容涉及价格/功能/文案时，按竞品情报视角解读影响；
4. 简洁口语化，用中文。"""

MAX_HISTORY = 8      # 保留最近 8 条文本对话作为上下文
REPORT_CHARS = 2000  # 注入雷达报告摘要的字符上限


def latest_report_text() -> str:
    mds = sorted(REPORT_DIR.glob("report_*.md"))
    if not mds:
        return "（暂无雷达报告：可先运行 python -m rival_eye.cli demo 生成）"
    return mds[-1].read_text(encoding="utf-8")[:REPORT_CHARS]


class AssistantAgent:
    def __init__(self):
        self.history: list[dict] = []

    def ask(self, question: str, on_delta=None) -> str:
        """一次提问：截桌面 → 组装上下文 → VLM 流式生成。on_delta(chunk) 供悬浮窗逐字回显。"""
        try:
            screen_uri = grab_screen_jpeg_uri()
        except Exception as e:
            if not VLM_API_KEY:
                screen_uri = None  # Mock 模式没有真实屏幕也能走通交互
            else:
                raise RuntimeError(f"桌面截图失败：{e}") from e

        parts = [{"type": "text", "text": f"【雷达最新报告摘要】\n{latest_report_text()}"}]
        if screen_uri:
            parts.append({"type": "image_url", "image_url": {"url": screen_uri}})
        parts.append({"type": "text", "text": f"【用户提问】{question}"})

        if not VLM_API_KEY:
            reply = self._mock_reply(question, has_screen=screen_uri is not None)
        else:
            from openai import OpenAI

            client = OpenAI(api_key=VLM_API_KEY, base_url=VLM_BASE_URL)
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages += self.history[-MAX_HISTORY:]
            messages.append({"role": "user", "content": parts})
            stream = client.chat.completions.create(
                model=VLM_MODEL, messages=messages, temperature=0.3, stream=True,
            )
            chunks = []
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    chunks.append(delta)
                    if on_delta:
                        on_delta(delta)
            reply = "".join(chunks)

        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def _mock_reply(self, question: str, has_screen: bool) -> str:
        screen_line = "已截取你的桌面画面 ✅" if has_screen else "截图不可用（无显示环境），仅载入雷达报告"
        return (
            f"【离线演示模式】{screen_line}\n"
            f"收到提问：{question}\n"
            "同时已载入最新雷达报告作为上下文。当前未配置 VLM_API_KEY，本条为演示文案——\n"
            "配置 Key 后，我将真正“看图”回答并逐字流式输出。"
        )
