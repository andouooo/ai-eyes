"""推送通知：飞书群机器人 webhook。未配置则静默跳过。"""
import requests

from .config import FEISHU_WEBHOOK


def push(text: str) -> bool:
    if not FEISHU_WEBHOOK:
        return False
    try:
        requests.post(
            FEISHU_WEBHOOK,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=10,
        )
        return True
    except requests.RequestException:
        return False
