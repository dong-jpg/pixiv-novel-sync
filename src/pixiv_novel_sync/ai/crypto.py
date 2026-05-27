from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


class AISecretError(RuntimeError):
    pass


@dataclass(slots=True)
class AISecretManager:
    env_name: str = "PIXIV_NOVEL_SYNC_AI_SECRET_KEY"

    def _fernet(self) -> Fernet:
        secret = os.getenv(self.env_name, "").strip()
        if not secret:
            raise AISecretError(f"缺少环境变量 {self.env_name}，无法加密或解密 API key")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        return Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        return self._fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str | None) -> str | None:
        if not ciphertext:
            return None
        try:
            return self._fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise AISecretError("API key 解密失败，请检查加密密钥或重新填写 API key") from exc
