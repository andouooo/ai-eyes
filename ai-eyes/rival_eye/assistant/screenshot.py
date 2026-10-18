"""桌面截图：用户主动提问那一刻截取全部显示器 → 压缩 JPEG data URI。

只截真实像素给模型看，不解析 DOM / 不读页面代码——Agent 的唯一信息来源就是"眼睛"。
"""
import base64
import io


def grab_box_image(box: dict, max_width: int | None = None):
    """按 mss 物理坐标框（left/top/width/height）截取任意区域，返回 PIL RGB 图。"""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        raw = sct.grab(box)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
    if max_width and img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)),
                         Image.Resampling.BILINEAR)
    return img


def grab_monitor_image(monitor_index: int = 0, max_width: int | None = None):
    """截取指定显示器（0 = 全部拼合），返回 PIL RGB 图。"""
    import mss

    with mss.mss() as sct:
        mons = sct.monitors
        box = mons[min(monitor_index, len(mons) - 1)]
    return grab_box_image(box, max_width)


def grab_screen_image(max_width: int | None = None):
    """截取全部显示器拼合画面，返回 PIL RGB 图。"""
    return grab_monitor_image(0, max_width)


def grab_screen_jpeg_uri(max_width: int = 1600, quality: int = 80) -> str:
    img = grab_screen_image(max_width)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
