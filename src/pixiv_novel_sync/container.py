"""NAS 容器入口：校验持久目录后启动现有单进程 CLI。"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

import yaml

from .settings import load_settings


def prepare_runtime(root: Path) -> tuple[Path, Path]:
    config_path = root / "config" / "config.yaml"
    env_path = root / "config" / ".env"
    for path in (config_path, env_path):
        if not path.is_file():
            raise RuntimeError(f"缺少 {path}；请按 docs/DOCKER.md 复制示例并配置后启动。")

    settings = load_settings(config_path, env_path)
    if not str(settings.dashboard_token or "").strip():
        raise RuntimeError("请在 config/.env 设置 DASHBOARD_TOKEN，供 NAS 浏览器访问时登录。")

    directories = {
        config_path.parent,
        settings.storage.db_path.parent,
        settings.storage.public_dir,
        settings.storage.private_dir,
    }
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        # 保存配置和归档依赖在同一目录创建临时文件再原子替换。
        with tempfile.TemporaryFile(dir=directory):
            pass
    return config_path, env_path


def main() -> None:
    try:
        config_path, env_path = prepare_runtime(Path.cwd())
    except yaml.YAMLError:
        # YAML 异常可能包含原文，配置里可能有 Cookie，不能原样写入容器日志。
        print("容器启动失败：config/config.yaml 格式无效，请检查 YAML 缩进和引号。", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"容器启动失败：{exc} 请检查挂载目录及 PIXIV_UID/PIXIV_GID 权限。", file=sys.stderr)
        raise SystemExit(1) from None
    command = [
        "pixiv-novel-sync", "--config", str(config_path), "--env-file", str(env_path),
        *sys.argv[1:],
    ]
    # 让信号直接到达服务进程，容器内仅有一个调度器。
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
