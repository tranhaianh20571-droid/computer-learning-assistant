"""凭据加密与掩码（切片 1 T02）。

- 主密钥从环境变量 `APP_ENCRYPTION_KEY` 读取，不落盘、不写日志。
- 使用 AES-256-GCM；密文格式 base64(nonce(12) || ciphertext)。
- 浏览器只收到掩码，永不返回完整凭据。
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENV_KEY = "APP_ENCRYPTION_KEY"
_NONCE_BYTES = 12


class CredentialCryptoError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _decode_key(raw: str) -> bytes:
    raw = raw.strip()
    # 优先按 base64 解析 32 字节密钥；否则用 SHA-256 派生，保证任意部署口令可用。
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            candidate = decoder(raw, validate=True)
            if len(candidate) == 32:
                return candidate
        except Exception:  # noqa: BLE001
            continue
    return hashlib.sha256(raw.encode("utf-8")).digest()


def load_key(explicit: Optional[str] = None) -> bytes:
    raw = explicit if explicit is not None else os.environ.get(ENV_KEY, "")
    if not raw:
        raise CredentialCryptoError("encryption_key_missing")
    return _decode_key(raw)


def encrypt(plaintext: str, key: Optional[bytes] = None) -> str:
    """加密凭据 JSON 字符串，返回 base64 密文。"""
    material = key or load_key()
    nonce = os.urandom(_NONCE_BYTES)
    blob = AESGCM(material).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + blob).decode("ascii")


def decrypt(ciphertext: str, key: Optional[bytes] = None) -> str:
    material = key or load_key()
    try:
        raw = base64.b64decode(ciphertext, validate=True)
        nonce, blob = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return AESGCM(material).decrypt(nonce, blob, None).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise CredentialCryptoError("credential_decrypt_failed") from exc


def mask_secret(secret: str) -> str:
    """只保留前后少量字符用于识别；不足 8 位全部遮蔽。"""
    if not secret:
        return "****"
    if len(secret) < 8:
        return "****"
    return f"{secret[:3]}****{secret[-4:]}"


def mask_credentials(credentials: dict) -> str:
    """把凭据字典掩码为单个可展示字符串（不暴露字段结构值）。"""
    for key in ("api_key", "token", "secret", "password", "access_token"):
        value = credentials.get(key)
        if isinstance(value, str) and value:
            return mask_secret(value)
    return "****"
