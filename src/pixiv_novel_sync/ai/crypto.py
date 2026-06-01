from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


class AISecretError(RuntimeError):
    pass


# 固定盐值（基于应用名派生，避免需要额外存储）
_APP_SALT = b"pixiv-novel-sync-ai-secret-v2"


@dataclass(slots=True)
class AISecretManager:
    env_name: str = "PIXIV_NOVEL_SYNC_AI_SECRET_KEY"
    _cache_secret_v1: str = field(default="", repr=False)
    _cache_secret_v2: str = field(default="", repr=False)
    _cache_fernet_v1: Fernet | None = field(default=None, repr=False)
    _cache_fernet_v2: Fernet | None = field(default=None, repr=False)

    def _get_secret(self) -> str:
        secret = os.getenv(self.env_name, "").strip()
        if not secret:
            raise AISecretError(f"缺少环境变量 {self.env_name}，无法加密或解密 API key")
        return secret

    def _fernet_v1(self) -> Fernet:
        """旧版 KDF：裸 SHA-256（向后兼容解密）。"""
        secret = self._get_secret()
        if secret != self._cache_secret_v1 or self._cache_fernet_v1 is None:
            key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
            self._cache_fernet_v1 = Fernet(key)
            self._cache_secret_v1 = secret
        return self._cache_fernet_v1

    def _fernet_v2(self) -> Fernet:
        """新版 KDF：PBKDF2-SHA256，480000 次迭代。"""
        secret = self._get_secret()
        if secret != self._cache_secret_v2 or self._cache_fernet_v2 is None:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=_APP_SALT,
                iterations=480_000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))
            self._cache_fernet_v2 = Fernet(key)
            self._cache_secret_v2 = secret
        return self._cache_fernet_v2

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        token = self._fernet_v2().encrypt(plaintext.encode("utf-8")).decode("utf-8")
        return f"v2${token}"

    def decrypt(self, ciphertext: str | None) -> str | None:
        if not ciphertext:
            return None
        try:
            if ciphertext.startswith("v2$"):
                raw = ciphertext[3:]
                return self._fernet_v2().decrypt(raw.encode("utf-8")).decode("utf-8")
            # 旧版密文（无前缀），使用 v1 KDF 解密
            return self._fernet_v1().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise AISecretError("API key 解密失败，请检查加密密钥或重新填写 API key") from exc
