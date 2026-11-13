# conftest：确保 pytest 运行时 backend 目录在 sys.path（从 backend 或项目根均可执行）
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
