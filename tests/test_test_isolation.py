from __future__ import annotations

import os
from pathlib import Path


def test_runtime_paths_are_isolated_from_repository(tmp_path: Path) -> None:
    for env_name in ("PIXIV_DB_PATH", "PIXIV_PUBLIC_DIR", "PIXIV_PRIVATE_DIR"):
        runtime_path = Path(os.environ.get(env_name, ".")).resolve()
        assert runtime_path.is_relative_to(tmp_path), f"{env_name} 未隔离到 tmp_path: {runtime_path}"
