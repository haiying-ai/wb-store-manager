from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# WB is one platform under the shared commerce-material root.  Keep category
# folders here so the existing 00知识库–04运营资料 protocol stays unchanged.
DEFAULT_BUSINESS_ROOT = Path("E:/电商运营/01平台资料/01WB")


@dataclass(frozen=True)
class CategoryPaths:
    category: str
    root: Path
    kb: Path
    source: Path
    generated: Path
    listing: Path
    ops: Path


def category_paths(category: str, *, business_root: Path | str = DEFAULT_BUSINESS_ROOT) -> CategoryPaths:
    if not category or not category.strip():
        raise ValueError("category is required")
    root = Path(business_root) / category.strip()
    return CategoryPaths(
        category=category.strip(),
        root=root,
        kb=root / "00知识库",
        source=root / "01来源资料",
        generated=root / "02生成资料",
        listing=root / "03上线资料",
        ops=root / "04运营资料",
    )


def ensure_category_dirs(paths: CategoryPaths) -> None:
    for folder in (paths.kb, paths.source, paths.generated, paths.listing, paths.ops):
        folder.mkdir(parents=True, exist_ok=True)


def timestamped_batch_dir(paths: CategoryPaths, sku: str, stamp: str) -> Path:
    if not sku or not sku.strip():
        raise ValueError("sku is required")
    return paths.generated / sku.strip() / stamp
