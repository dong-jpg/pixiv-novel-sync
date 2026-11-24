"""Verify installed resources and branch route boundaries from an isolated wheel."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


SMOKE = r'''
import base64
import hashlib
import importlib
import os
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
writing = sys.argv[2] == "writing"
sys.path.insert(0, str(root))
package = importlib.import_module("pixiv_novel_sync")
assert Path(package.__file__).resolve().is_relative_to(root)
for module in ("storage.preference_sources", "preference_statistics", "sync.rescan", "container"):
    importlib.import_module("pixiv_novel_sync." + module)

runtime = root / "runtime"
runtime.mkdir()
os.environ.update(PIXIV_DB_PATH=str(runtime / "library.db"),
                  PIXIV_PUBLIC_DIR=str(runtime / "public"),
                  PIXIV_PRIVATE_DIR=str(runtime / "private"))
from pixiv_novel_sync.webapp import create_app
app = create_app(config_path=str(runtime / "missing.yaml"),
                 env_path=str(runtime / "missing.env"), start_scheduler=False)
client = app.test_client()
checks = 0
for path in ("/dashboard", "/dashboard/novels", "/dashboard/preferences",
             "/dashboard/preferences?view=work-analysis", "/dashboard/preferences?view=learning",
             "/dashboard/settings/sync", "/dashboard/settings/models",
             "/dashboard/settings/agents", "/dashboard/settings/system"):
    with client.get(path) as response:
        assert response.status_code == 200, (path, response.status_code)
    checks += 1
for path in ("/dashboard/ai", "/dashboard/wizard", "/dashboard/settings/adult"):
    with client.get(path) as response:
        assert response.status_code == (200 if writing else 404), (path, response.status_code)
    checks += 1
assets = root / "pixiv_novel_sync" / "static"
vue = (assets / "vendor/vue-3.4.38.global.prod.js").read_bytes()
template = client.get("/dashboard").get_data(as_text=True)
integrity = base64.b64encode(hashlib.sha384(vue).digest()).decode()
assert 'integrity="sha384-' + integrity + '"' in template
assert "The MIT License" in (assets / "vendor/vue-LICENSE.txt").read_text(encoding="utf-8")
assert (assets / "css/tailwind.css").stat().st_size > 0
checks += 3
print(f"{checks} installed route/resource checks passed ({sys.argv[2]})")
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--writing", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pixiv-wheel-") as directory:
        with zipfile.ZipFile(args.wheel) as archive:
            names = archive.namelist()
            for name in names:
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise ValueError("Unexpected wheel path")
            assert not any("pixiv_novel_sync/remote/" in name or "pixiv-remote" in name for name in names)
            archive.extractall(directory)
        environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith(("PIXIV_", "DASHBOARD_"))
            and name not in {"ENV_PATH", "PYTHONPATH"}
        }
        result = subprocess.run(
            [sys.executable, "-I", "-c", SMOKE, directory, "writing" if args.writing else "main"],
            cwd=directory, env=environment, timeout=90, check=False,
        )
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
