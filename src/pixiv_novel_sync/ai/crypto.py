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


# 固定盐值（基于应用名派生，避免需要额外存储）。
# 权衡说明 (L4)：使用应用级固定盐意味着相同 secret 在所有安装上派生出相同密钥，
# 失去 per-install 唯一性、理论上可被预计算。真正的机密是环境变量
# PIXIV_NOVEL_SYNC_AI_SECRET_KEY，且 PBKDF2 迭代 480k 已使暴力破解代价高昂；
# 更换为随机 per-install 盐会使全部存量 v2 密文失效，收益（Low）不抵迁移风险，
# 故保持固定盐。若未来需要，应引入 v3 KDF + 存储随机盐并做透明升级。
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

    @staticmethod
    def is_legacy_ciphertext(ciphertext: str | None) -> bool:
        """判断密文是否用已废弃的 v1（无盐 SHA-256）KDF 加密。

        L4：v1 用裸 SHA-256 派生 Fernet 密钥，低熵 secret 下可离线爆破。调用方
        可在成功解密后据此透明地用 v2 重新加密并回写，逐步淘汰 v1 密文。
        """
        return bool(ciphertext) and not ciphertext.startswith("v2$")

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
