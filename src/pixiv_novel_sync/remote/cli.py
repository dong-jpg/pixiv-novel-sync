"""Separate process entrypoint; never loads the application's dotenv/settings."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import random
import re
import signal
import sys
import threading
import time
from typing import Mapping

from .config import RemoteConfig
from .contracts import RemoteError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse ordinarily repeats arbitrary command arguments. Keep failures
        # redacted, including accidental pastes of credentials.
        raise RemoteError("invalid_arguments", 400)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="pixiv-remote", description="Dedicated remote execution processes")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for role in ("controller", "worker"):
        command = commands.add_parser(role)
        command.add_argument("--once", action="store_true", help="perform one supervision cycle and exit")
    doctor = commands.add_parser("doctor", help="offline configuration and directory checks")
    doctor.add_argument("--role", choices=("web", "controller", "worker"), default="web")
    doctor.add_argument("--network", action="store_true", help="read controller's machine status without changing it")
    commands.add_parser('egress',help='run the explicitly configured cloud TLS CONNECT exit')
    for name in ("recover", "gc"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", required=True)
        command.add_argument("--confirm-state-dir", required=True)
        if name == "recover":
            command.add_argument("--rotate-epoch", action="store_true", required=True)
            command.add_argument("--api-offline", action="store_true", required=True,
                                 help="attest that every Web worker API process is stopped/disabled")
    return parser


@contextmanager
def _signals(stop: threading.Event):
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stop.set())
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _doctor(cfg: RemoteConfig, network: bool = False) -> int:
    if network and cfg.role != "controller":
        raise RemoteError("controller_role_required", 400)
    if not cfg.enabled:
        print("remote disabled")
        return 0
    root = cfg.work_dir if cfg.role == "worker" else cfg.state_dir
    if root.exists():
        if root.is_symlink() or not root.is_dir() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            raise RemoteError("directory_not_accessible", 400)
        if os.name != "nt" and root.stat().st_mode & 0o007:
            raise RemoteError("directory_permissions", 400)
        directory = "accessible"
    else:
        ancestor = root.parent
        while not ancestor.exists() and ancestor.parent != ancestor:
            ancestor = ancestor.parent
        if not os.access(ancestor, os.W_OK | os.X_OK):
            raise RemoteError("directory_not_accessible", 400)
        directory = "creation_required"
    result = {"role": cfg.role, "configuration": "valid", "directory": directory,
              "network": "not_checked"}
    if network:
        from .instabox import BASE_URL, InstaBoxClient, ProviderError

        provider = InstaBoxClient(cfg.api_key, cfg.machine_id)
        started = time.monotonic()
        try:
            machine = provider.machine()
            result.update(network="reachable", provider_state=machine.state, runtime_ready=machine.runtime_ready)
        except ProviderError as error:
            result.update(network="failed", error=error.code, http_status=error.http_status,
                          reason=error.network_reason)
        finally:
            provider.close()
        result.update(endpoint=BASE_URL, proxy="disabled", tls_verification=True,
                      elapsed_ms=round((time.monotonic() - started) * 1000))
    print(json.dumps(result, sort_keys=True))
    if result["network"] == "failed":
        return 1
    return 0


def _maintenance(args, env: Mapping[str, str]) -> int:
    from .blobs import BlobStore
    from .maintenance import collect_garbage, recover
    from .store import RemoteStore

    root, confirmed = Path(args.state_dir), Path(args.confirm_state_dir)
    if (not root.is_absolute() or root.parent == root or not confirmed.is_absolute()
            or root.is_symlink() or root.resolve() != root.absolute()
            or root.resolve() != confirmed.resolve()):
        raise RemoteError("state_directory_confirmation_required", 400)
    db = root / "control.db"
    if db.is_symlink() or not db.is_file():
        raise RemoteError("control_database_missing", 400)
    configured = env.get("PIXIV_REMOTE_STATE_DIR")
    if configured and Path(configured).resolve() != root.resolve():
        raise RemoteError("state_directory_mismatch", 400)
    business = env.get("PIXIV_DB_PATH")
    if business and Path(business).resolve() == db.resolve():
        raise RemoteError("state_overlaps_library", 400)
    machine_id = env.get("PIXIV_REMOTE_MACHINE_ID", "")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", machine_id) is None:
        raise RemoteError("invalid_machine_id", 400)
    if args.command == "recover" and env.get("PIXIV_REMOTE_ENABLED", "0") != "0":
        raise RemoteError("remote_api_must_be_offline", 409)
    store = RemoteStore(root, machine_id)
    try:
        store.migrate()
        if args.command == "recover":
            recover(store, time.time())
            print("remote epoch rotated; replace bootstrap credentials before enabling the API")
        else:
            result = collect_garbage(store, BlobStore(root / "blobs"), time.time())
            print(json.dumps({"gc": result}, sort_keys=True))
    finally:
        store.close()
    return 0


def _serve(args, cfg: RemoteConfig) -> int:
    if not cfg.enabled:
        print("remote disabled")
        return 0
    store = provider = process = None
    if args.command == "worker":
        from .worker import Worker
        from .worker_http import WorkerHTTP

        process = Worker(WorkerHTTP(cfg.server_origin, cfg.worker_key), cfg.work_dir)
    else:
        from .control_store import ControlStore
        from .instabox import InstaBoxClient
        from .lifecycle import Controller
        from .store import RemoteStore

        store = RemoteStore(cfg.state_dir, cfg.machine_id)
        store.migrate()
        provider = InstaBoxClient(cfg.api_key, cfg.machine_id)
        process = Controller(store, ControlStore(store), provider, clock=time.time,
                             jitter=lambda: random.uniform(0, 5))
    stop = threading.Event()
    try:
        with _signals(stop):
            while not stop.is_set():
                next_tick = process.tick()
                if args.once:
                    error = (process.control.snapshot(time.time())["last_error"]
                             if args.command == "controller" else process.last_error)
                    return 1 if error else 0
                # The controller's returned schedule bounds operation polls as
                # well as mutations. The worker supervises ongoing transfers
                # every second; both waits remain interruptible by SIGTERM.
                maximum_wait = 60.0 if args.command == "controller" else 1.0
                stop.wait(max(0.05, min(maximum_wait, next_tick - time.time())))
    finally:
        if args.command == "worker" and process is not None:
            process.stop()
        if provider is not None and hasattr(provider, "close"):
            provider.close()
        if store is not None:
            store.close()
    return 0


def run_cli(argv: list[str], env: Mapping[str, str] | None = None) -> int:
    values = dict(os.environ if env is None else env)
    try:
        args = _parser().parse_args(argv)
        if args.command=='egress':
            from .egress import serve
            serve(values)
            return 0
        if args.command in {"recover", "gc"}:
            return _maintenance(args, values)
        cfg = RemoteConfig.from_env(values, role=args.role if args.command == "doctor" else args.command)
        return _doctor(cfg, args.network) if args.command == "doctor" else _serve(args, cfg)
    except SystemExit as exc:
        return int(exc.code or 0)
    except RemoteError as exc:
        print("remote: " + exc.code, file=sys.stderr)
        return 2 if exc.code == "invalid_arguments" else 1
    except (OSError, ValueError):
        print("remote: local_runtime_error", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run_cli(sys.argv[1:]))


if __name__ == "__main__":
    main()
