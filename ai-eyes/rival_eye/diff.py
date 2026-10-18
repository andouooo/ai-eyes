"""语义级 diff：无 API Key 自动降级 Mock 模式，保证离线可演示、现场不翻车。"""
import base64
import json
from pathlib import Path

from .config import DEMO_DIR, VLM_API_KEY, VLM_BASE_URL, VLM_MODEL

PROMPT = """你是竞品视觉雷达的核心分析引擎。给你同一竞品网页昨天(图1)和今天(图2)的截图。
找出两天之间的全部有意义变化（价格、文案、功能、布局），忽略轮播图、时钟等噪声。
只输出 JSON，不要输出任何其他文字：
{"changes":[{"type":"价格|文案|功能|布局","before":"变化前","after":"变化后","impact":"高|中|低","box":{"x":0,"y":0,"w":0,"h":0},"note":"对竞争态势的影响与建议动作，一句话"}],"summary":"今天最值得警惕的一句话结论"}
box 是变化区域在 1280x800 截图上的像素坐标，没有精确位置可省略 box。"""


def _data_uri(png: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(Path(png).read_bytes()).decode()


def semantic_diff(png_a: Path, png_b: Path) -> dict:
    if not VLM_API_KEY:
        mock = DEMO_DIR / "mock_diff.json"
        data = json.loads(mock.read_text(encoding="utf-8"))
        data["_mode"] = "mock"
        return data

    from openai import OpenAI

    client = OpenAI(api_key=VLM_API_KEY, base_url=VLM_BASE_URL)
    resp = client.chat.completions.create(
        model=VLM_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": _data_uri(png_a)}},
                {"type": "image_url", "image_url": {"url": _data_uri(png_b)}},
            ],
        }],
        temperature=0.1,
    )
    text = resp.choices[0].message.content or ""
    text = text[text.find("{"): text.rfind("}") + 1]
    data = json.loads(text)
    data["_mode"] = "vlm"
    return data
