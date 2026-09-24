from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_nginx_config_enables_strict_https() -> None:
    config = (ROOT / "config/nginx/pixiv-novel-sync.conf").read_text(
        encoding="utf-8"
    )

    assert config.count("server_name ${PIXIV_SERVER_NAME};") == 2
    assert "listen 80;" in config
    assert "return 301 https://${PIXIV_SERVER_NAME}$request_uri;" in config
    assert "listen 443 ssl;" in config
    assert "ssl_certificate ${PIXIV_SSL_CERTIFICATE};" in config
    assert "ssl_certificate_key ${PIXIV_SSL_CERTIFICATE_KEY};" in config
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in config
    assert "proxy_pass http://127.0.0.1:5011;" in config
    # 只替换这三个占位符，nginx 自己的 $host 不能被 envsubst 吃掉
    assert "$host" in config


def test_update_script_reports_public_https_url() -> None:
    script = (ROOT / "update.sh").read_text(encoding="utf-8")

    assert 'PUBLIC_URL="https://pixiv.dongboapp.com"' in script
    assert 'echo "访问地址: ${PUBLIC_URL}"' in script
