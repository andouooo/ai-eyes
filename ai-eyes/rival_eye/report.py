"""报告生成：HTML（红框标注 + 前后对比）+ Markdown 摘要。"""
import datetime
import os
from pathlib import Path

from .config import REPORT_DIR

IMPACT_COLOR = {"高": "#e5484d", "中": "#f5a623", "低": "#3b82f6"}

# .scaler 按 1280x800 原始坐标布局，再整体缩放放入卡片，红框坐标因此无需换算
TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>{title}</title>
<style>
 body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f6fb;margin:0;padding:32px;color:#1a2233;}}
 .wrap{{max-width:1320px;margin:0 auto;}}
 h1{{font-size:26px;}} .meta{{color:#6b7280;font-size:13px;margin:6px 0 18px;}}
 .badge{{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;background:#eef2ff;color:#4f46e5;margin-left:8px;vertical-align:middle;}}
 .summary{{background:#fff;border-left:4px solid #e5484d;border-radius:8px;padding:14px 18px;font-size:15px;line-height:1.7;box-shadow:0 1px 4px rgba(0,0,0,.05);}}
 table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;margin:22px 0;box-shadow:0 1px 4px rgba(0,0,0,.05);font-size:14px;}}
 th{{background:#141b2d;color:#fff;text-align:left;padding:10px 12px;font-weight:600;}}
 td{{padding:10px 12px;border-top:1px solid #eef0f4;line-height:1.6;vertical-align:top;}}
 .shots{{display:flex;gap:24px;}}
 .shot{{width:620px;height:414px;overflow:hidden;background:#fff;border-radius:10px;padding:8px;box-shadow:0 1px 4px rgba(0,0,0,.05);box-sizing:border-box;}}
 .shot h4{{margin:0 0 6px;font-size:14px;color:#6b7280;font-weight:600;}}
 .scaler{{width:1280px;height:800px;position:relative;transform:scale(0.471875);transform-origin:top left;}}
 .scaler img{{width:1280px;display:block;border-radius:6px;}}
 .frame-tag{{position:absolute;left:0;bottom:0;background:#e5484d;color:#fff;font-size:18px;padding:6px 16px;border-radius:0 6px 0 0;z-index:9;}}
</style></head><body><div class="wrap">
<h1>{title}<span class="badge">{mode}</span></h1>
<div class="meta">生成时间：{ts} · 竞眼雷达 RivalEye</div>
<div class="summary"><b>🚨 AI 巡检结论：</b>{summary}</div>
<table><tr><th>#</th><th>类型</th><th>昨天</th><th>今天</th><th>影响</th><th>AI 解读</th></tr>{rows}</table>
<div class="shots">
 <div class="shot"><h4>昨天 · {name_a}</h4><div class="scaler"><div class="frame-tag">昨天</div><img src="{rel_a}"></div></div>
 <div class="shot"><h4>今天 · {name_b}（红框 = AI 标注变化）</h4><div class="scaler"><div class="frame-tag">今天</div><img src="{rel_b}">{boxes}</div></div>
</div>
</div></body></html>"""


def _boxes(changes: list) -> str:
    parts = []
    for c in changes:
        b = c.get("box")
        if not b:
            continue
        color = IMPACT_COLOR.get(c.get("impact", "中"), "#f5a623")
        parts.append(
            f'<div style="position:absolute;left:{b["x"]}px;top:{b["y"]}px;'
            f'width:{b["w"]}px;height:{b["h"]}px;border:3px solid {color};'
            f'border-radius:6px;"></div>'
        )
    return "\n".join(parts)


def _rows(changes: list) -> str:
    rows = []
    for i, c in enumerate(changes, 1):
        color = IMPACT_COLOR.get(c.get("impact", "中"), "#f5a623")
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td><b>{c.get('type', '')}</b></td>"
            f"<td>{c.get('before', '')}</td>"
            f"<td><b>{c.get('after', '')}</b></td>"
            f'<td style="color:{color};font-weight:700">{c.get("impact", "")}</td>'
            f"<td>{c.get('note', '')}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def build(d: dict, png_a: Path, png_b: Path, title: str) -> tuple[Path, Path]:
    ts = datetime.datetime.now()
    mode = "VLM 实时分析" if d.get("_mode") == "vlm" else "离线 Mock（未配置 VLM_API_KEY）"
    html = TEMPLATE.format(
        title=title,
        mode=mode,
        ts=f"{ts:%Y-%m-%d %H:%M:%S}",
        summary=d.get("summary", ""),
        rows=_rows(d.get("changes", [])),
        boxes=_boxes(d.get("changes", [])),
        name_a=png_a.name,
        name_b=png_b.name,
        rel_a=os.path.relpath(png_a, REPORT_DIR),
        rel_b=os.path.relpath(png_b, REPORT_DIR),
    )

    lines = [
        f"# {title}",
        "",
        f"- 生成时间：{ts:%Y-%m-%d %H:%M:%S}（{mode}）",
        f"- 巡检结论：{d.get('summary', '')}",
        "",
    ]
    for i, c in enumerate(d.get("changes", []), 1):
        lines.append(
            f"{i}. **[{c.get('impact', '')}影响 · {c.get('type', '')}]** "
            f"{c.get('before', '')} → **{c.get('after', '')}** — {c.get('note', '')}"
        )

    html_path = REPORT_DIR / f"report_{ts:%Y%m%d_%H%M%S}.html"
    md_path = REPORT_DIR / f"report_{ts:%Y%m%d_%H%M%S}.md"
    html_path.write_text(html, encoding="utf-8")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return html_path, md_path
