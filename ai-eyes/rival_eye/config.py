"""全局配置：路径、视口、VLM 与推送配置（均可用环境变量覆盖）。"""
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：可写数据（快照/报告）放 exe 旁边，只读资源在解包临时目录
    ROOT = Path(sys.executable).resolve().parent
    ASSET_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parent.parent
    ASSET_ROOT = ROOT

SNAP_DIR = ROOT / "snapshots"
REPORT_DIR = ROOT / "reports"
DEMO_DIR = ASSET_ROOT / "demo"


def _load_dotenv(path: Path) -> None:
    """读取 exe 旁边的 .env（KEY=VALUE，# 开头为注释），已有环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")

# 固定视口：红框坐标基于 1280x800，改视口需同步调整 demo 页面与 mock 坐标
VIEWPORT = {"width": 1280, "height": 800}

VLM_API_KEY = os.environ.get("VLM_API_KEY", "")
VLM_BASE_URL = os.environ.get("VLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
VLM_MODEL = os.environ.get("VLM_MODEL", "glm-4v-flash")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

SNAP_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)
