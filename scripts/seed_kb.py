# =============================================================================
# 法律知识库索引 CLI - seed_kb.py
# =============================================================================
# 用法（项目根目录）:
#   python scripts/seed_kb.py
# 从 backend/data/kb/ 加载语料（pdf/docx/txt/md）→ 分块 → 嵌入 → 入库。
# md5 稳定 ID：重复执行幂等（已入库的分块自动跳过）。
# =============================================================================

import sys
from pathlib import Path

# 将 backend 加入 sys.path（允许以"脚本方式"直接运行）
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from config.settings import settings  # noqa: E402


def main() -> None:
    settings.ensure_dirs()
    print("=" * 60)
    print("  SmartReview 法律知识库构建")
    print("=" * 60)
    print(f"\n语料目录：{settings.kb_dir_path}")
    print(f"向量持久化：{settings.chroma_dir_path}\n")

    from rag.store import kb
    result = kb.build_index()

    print(f"本次新增分块：{result.get('added', 0)}")
    print(f"知识库总条数：{result.get('total', 0)}")
    if result.get("message"):
        print(f"提示：{result['message']}")
    stats = kb.stats()
    if stats.get("sources"):
        print("\n来源分布：")
        for src, n in sorted(stats["sources"].items()):
            print(f"  - {Path(src).name}: {n} 条")


if __name__ == "__main__":
    main()
