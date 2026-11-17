"""在 Linux 临时目录核对远程服务示例；仅使用回环地址和合成数据。"""
from __future__ import annotations

import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def checked(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{args[0]}: {result.stderr.strip()}")
    return result


def verify_units(directory: Path) -> dict:
    units = directory / "units"
    units.mkdir()
    names = [f"pixiv-remote-{role}.service" for role in ("controller", "worker")]
    for name in names:
        shutil.copyfile(ROOT / "deploy/systemd" / name, units / name)
    env = dict(os.environ, SYSTEMD_UNIT_PATH=f"{units}:/usr/lib/systemd/system:/lib/systemd/system")
    args = ["systemd-analyze", "--man=no", "--generators=no", "verify"]
    args.extend(str(units / name) for name in names)
    exact = subprocess.run(args, capture_output=True, text=True, timeout=30, env=env)
    if exact.returncode:
        expected = "Command /opt/pixiv-novel-sync/app/.venv/bin/pixiv-remote is not executable: No such file or directory"
        lines = exact.stderr.strip().splitlines()
        if len(lines) != 2 or any(expected not in line for line in lines):
            raise RuntimeError(f"systemd unit validation: {exact.stderr.strip()}")
    # 原 unit 字节不变；临时 drop-in 只替换尚未安装的程序路径，不运行它。
    for role, name in zip(("controller", "worker"), names):
        dropin = units / f"{name}.d"
        dropin.mkdir()
        (dropin / "verification.conf").write_text(
            f"[Service]\nExecStart=\nExecStart=/usr/bin/true {role}\n", encoding="utf-8"
        )
    checked(args, env=env)
    return {
        "syntax_with_executable_fixture": "passed",
        "exact_unit_exit_code": exact.returncode,
        "exact_unit_diagnostics": exact.stderr.strip(),
        "scope": "只验证 unit 指令；安装路径、用户、权限与冷启动必须在目标主机核对",
    }


class SyntheticUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    events: list[dict] = []
    upload_started = threading.Event()

    def log_message(self, *args):
        pass

    def do_POST(self):
        self._respond()

    def do_PUT(self):
        self.upload_started.set()
        self._respond()

    def _respond(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        event = {
            "path": self.path,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "accept_encoding": self.headers.get("Accept-Encoding"),
            "connection": self.headers.get("Connection"),
            "forwarded_for": self.headers.get("X-Forwarded-For"),
            "real_ip": self.headers.get("X-Real-IP"),
            "forwarded_proto": self.headers.get("X-Forwarded-Proto"),
        }
        self.events.append(event)
        data = json.dumps({"observed": event, "padding": "x" * 2048}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(data)


def verify_nginx(directory: Path, nginx: str) -> dict:
    # 所有服务、证书及 HTTP 输入均为本次验证创建，不加载书库或真实配置。
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticUpstream)
    upstream.daemon_threads = True
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    certificate, private_key = directory / "fixture.crt", directory / "fixture.key"
    checked([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        "-keyout", str(private_key), "-out", str(certificate),
    ])
    private_key.chmod(0o600)
    snippet = (ROOT / "config/nginx/remote-worker-location.conf").read_text(encoding="utf-8")
    snippet = snippet.replace("http://127.0.0.1:5011", f"http://127.0.0.1:{upstream.server_port}")
    (directory / "locations.conf").write_text(snippet, encoding="utf-8")
    for child in ("body", "proxy", "fastcgi", "uwsgi", "scgi"):
        (directory / child).mkdir()
    config = directory / "nginx.conf"
    config.write_text(f"""daemon off;
master_process off;
pid "{directory}/nginx.pid";
error_log "{directory}/error.log" notice;
events {{ worker_connections 64; }}
http {{
    access_log off;
    client_body_temp_path "{directory}/body";
    proxy_temp_path "{directory}/proxy";
    fastcgi_temp_path "{directory}/fastcgi";
    uwsgi_temp_path "{directory}/uwsgi";
    scgi_temp_path "{directory}/scgi";
    limit_req_zone $binary_remote_addr zone=pixiv_remote_sessions:1m rate=6r/m;
    gzip on;
    gzip_min_length 1;
    gzip_types application/json;
    server {{
        listen 127.0.0.1:{port} ssl;
        server_name localhost;
        ssl_certificate "{certificate}";
        ssl_certificate_key "{private_key}";
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
        include "{directory}/locations.conf";
        location = /health {{ return 200 "ok"; }}
    }}
}}
""", encoding="utf-8")
    checked([nginx, "-t", "-p", str(directory), "-c", str(config)])
    context = ssl.create_default_context(cafile=str(certificate))

    def connection():
        return http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)

    def request(method, path, data=b"{}"):
        conn = connection()
        try:
            conn.request(method, path, body=data, headers={
                "Accept-Encoding": "gzip",
                "X-Forwarded-For": "198.51.100.7, 203.0.113.9",
                "X-Real-IP": "192.0.2.8",
                "X-Forwarded-Proto": "http",
            })
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def assert_identity(path):
        status, headers, body = request("POST", path)
        assert status == 200, (path, status)
        assert "Content-Encoding" not in headers
        observed = json.loads(body)["observed"]
        assert observed["accept_encoding"] == "identity"
        assert observed["connection"] is None
        assert observed["forwarded_for"] == "127.0.0.1", observed["forwarded_for"]
        assert observed["real_ip"] == "127.0.0.1", observed["real_ip"]
        assert observed["forwarded_proto"] == "https"
        assert headers["Cache-Control"] == "private, no-store"
        assert headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"

    process = subprocess.Popen(
        [nginx, "-p", str(directory), "-c", str(config)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                if request("GET", "/health")[0] == 200:
                    break
            except OSError:
                pass
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("isolated_nginx_start_failed")
            time.sleep(0.05)
        assert_identity("/api/remote-worker/v1/sessions")
        assert_identity("/api/remote-worker/v1/session-heartbeat")

        payload = b"synthetic-upload" * (MIB // 16)
        conn = connection()
        try:
            conn.putrequest("PUT", "/api/remote-worker/v1/attempts/fixture/result")
            conn.putheader("Content-Length", str(len(payload)))
            conn.endheaders()
            conn.send(payload[:4096])
            assert SyntheticUpstream.upload_started.wait(3), "nginx buffered the request body"
            # 上传尚未结束，独立心跳请求仍能通过代理。
            assert_identity("/api/remote-worker/v1/session-heartbeat")
            conn.send(payload[4096:])
            response = conn.getresponse()
            assert response.status == 200
            result = json.loads(response.read())["observed"]
            assert result["size"] == len(payload)
            assert result["sha256"] == hashlib.sha256(payload).hexdigest()
        finally:
            conn.close()

        for path, limit in (("/api/remote-worker/v1/sessions", 16 * 1024),
                            ("/api/remote-worker/v1/attempts/fixture/result", 33 * MIB)):
            before = len(SyntheticUpstream.events)
            conn = connection()
            try:
                conn.putrequest("POST", path)
                conn.putheader("Content-Length", str(limit + 1))
                conn.endheaders()
                response = conn.getresponse()
                assert response.status == 413
                response.read()
                assert len(SyntheticUpstream.events) == before
            finally:
                conn.close()

        statuses = []
        for _ in range(8):
            status, headers, body = request("POST", "/api/remote-worker/v1/sessions")
            statuses.append(status)
            if status == 429:
                assert headers["Retry-After"] == "10"
                assert headers["Cache-Control"] == "private, no-store"
                assert headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
                assert json.loads(body) == {"error": "rate_limited"}
                break
        assert 429 in statuses
        # 注册限速使用自己的桶，不阻塞已有会话的心跳。
        assert_identity("/api/remote-worker/v1/session-heartbeat")
        return {
            "full_config_syntax": "passed",
            "tls_identity_both_locations": "passed",
            "verified_client_address_both_locations": "passed",
            "https_policy_including_bootstrap_429": "passed",
            "upload_streaming_with_concurrent_heartbeat": "passed",
            "bootstrap_16k_and_upload_33m_limits": "passed",
            "bootstrap_429_retry_after_and_heartbeat_isolation": "passed",
            "scope": "完整合成 TLS 配置，回环 upstream；未验证真实 VPS 路径或云端网络",
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nginx", default=shutil.which("nginx"))
    parser.add_argument("--output", type=Path, help="可选 JSON 证据文件；不保留合成私钥")
    args = parser.parse_args()
    if sys.platform != "linux" or not args.nginx:
        parser.error("需要 Linux、nginx、systemd-analyze 与 openssl；不会自动安装或启用服务")
    result = {
        "systemd_version": checked(["systemd-analyze", "--version"]).stdout.splitlines()[0],
        "nginx_version": checked([args.nginx, "-v"]).stderr.strip(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "deploy/systemd/pixiv-remote-controller.service",
                "deploy/systemd/pixiv-remote-worker.service",
                "config/nginx/remote-worker-location.conf",
            )
        },
    }
    with tempfile.TemporaryDirectory(prefix="pixiv-remote-verify-") as temp:
        directory = Path(temp)
        result["systemd"] = verify_units(directory)
        result["nginx"] = verify_nginx(directory, args.nginx)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
