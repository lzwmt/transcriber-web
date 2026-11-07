"""转写项目的固定验证脚本包。

Playwright 只在 verify 依赖分组。从仓库根目录运行：

    uv run --group verify python -m verify.run self-check
    uv run --group verify python -m verify.run v2a
    uv run --group verify python -m verify.run v2b
    uv run --group verify python -m verify.run polling
    uv run --group verify python -m verify.run tokens
    uv run --group verify python -m verify.run pack
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "verify-out"
TMP_DIR = ROOT / ".verify-tmp"
ITEMS_DIR = OUT_DIR / "items"
SELF_CHECK_DIR = OUT_DIR / "self-check"
STYLE_PATH = ROOT / "app" / "static" / "style.css"
TOKENS_PATH = ROOT / "docs" / "design-tokens.md"
CHECKLIST_PATH = ROOT / "docs" / "VERIFY-PILOT-CHECKLIST.md"
LIST_TEMPLATE = ROOT / "app" / "templates" / "list.html"

PORT_START = 8911
PORT_SPAN = 20
PRODUCTION_PORT = 8900
DESKTOP_WIDTH = 1280
DESKTOP_HEIGHT = 800
MOBILE_WIDTH = 390
MOBILE_HEIGHT = 844
