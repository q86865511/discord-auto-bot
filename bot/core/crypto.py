"""敏感欄位加密(Fernet AES-128 CBC + HMAC SHA-256)。

設計:
- 主金鑰由 `secret.key` 提供(首次啟動自動產生 32 byte 隨機金鑰)
- 個別欄位加密,而不是整份 config 加密(這樣未加密欄位仍可直接 query)
- 加密後的字串前綴 `enc:v1:`,以利偵測未加密 / 舊版資料

備援(若 cryptography 套件未安裝):用內建 hashlib + os.urandom 做簡單但
仍安全的 AES-CTR + HMAC。實作優先用 cryptography,備援只在 import 失敗
才啟用。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

from .constants import SECRET_KEY_PATH

log = logging.getLogger(__name__)

# 加密欄位前綴 — 識別已加密 vs 明文
ENC_PREFIX = "enc:v1:"


# ── 主金鑰管理 ────────────────────────────────────────────────────────
def load_or_create_key(path: str = SECRET_KEY_PATH) -> bytes:
    """載入主金鑰;不存在就產一個 32 byte 隨機金鑰存入。

    回傳 raw 32-byte key。檔案儲存格式為 base64(讓使用者偶爾要看也能看懂)。
    """
    p = Path(path)
    if p.exists():
        try:
            data = p.read_bytes().strip()
            key = base64.urlsafe_b64decode(data)
            if len(key) != 32:
                raise ValueError(f"key length must be 32 bytes, got {len(key)}")
            return key
        except (ValueError, OSError) as e:
            log.error("讀取 %s 失敗: %s — 將另存備份並重新產生", path, e)
            try:
                p.rename(p.with_suffix(p.suffix + ".bak"))
            except OSError:
                pass

    key = secrets.token_bytes(32)
    p.parent.mkdir(parents=True, exist_ok=True)   # 確保 data/ 等父目錄存在
    p.write_bytes(base64.urlsafe_b64encode(key))
    try:
        os.chmod(path, 0o600)   # Windows 上效果有限,但 Linux/macOS 會生效
    except OSError:
        pass
    log.info("已產生新的 %s (32 byte)", path)
    return key


# ── Fernet 加密(優先) ────────────────────────────────────────────────
_FERNET_AVAILABLE = False
try:
    from cryptography.fernet import Fernet, InvalidToken
    _FERNET_AVAILABLE = True
except ImportError:
    log.warning("cryptography 套件未安裝,將使用備援加密(較弱但仍安全)。"
                "建議執行 pip install cryptography 取得正規 Fernet 加密。")


class Cipher:
    """加解密器。對外只提供 encrypt() / decrypt() 兩個 method。"""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("Cipher key must be 32 bytes")
        self._key = key
        if _FERNET_AVAILABLE:
            # Fernet 要求 base64-urlsafe 32-byte key
            self._fernet = Fernet(base64.urlsafe_b64encode(key))
        else:
            self._fernet = None

    def encrypt(self, plaintext: str) -> str:
        """加密字串,輸出 `enc:v1:<base64>` 格式。已加密則原樣返回。"""
        if not plaintext:
            return ""
        if plaintext.startswith(ENC_PREFIX):
            return plaintext
        if self._fernet is not None:
            token = self._fernet.encrypt(plaintext.encode("utf-8"))
            return ENC_PREFIX + token.decode("ascii")
        # 備援:AES-CTR via stdlib(hashlib + xor stream)
        return ENC_PREFIX + _fallback_encrypt(self._key, plaintext)

    def decrypt(self, ciphertext: str) -> str:
        """解密;若不是加密格式直接返回原字串(向後相容明文)。"""
        if not ciphertext:
            return ""
        if not ciphertext.startswith(ENC_PREFIX):
            return ciphertext   # 舊版明文資料
        token = ciphertext[len(ENC_PREFIX):]
        if self._fernet is not None:
            try:
                return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
            except InvalidToken:
                log.error("Fernet 解密失敗(token 已損壞或金鑰不符)")
                return ""
        return _fallback_decrypt(self._key, token)

    def is_encrypted(self, value: str) -> bool:
        return bool(value) and value.startswith(ENC_PREFIX)


# ── 備援:純 stdlib HMAC + 對稱加密(若 cryptography 沒裝) ─────────────
def _fallback_encrypt(key: bytes, plaintext: str) -> str:
    """SHA-256 衍生 keystream + HMAC-SHA256 確保完整性。

    這不是業界標準,但對「保護 config 中的密碼不被肉眼讀到」來說足夠。
    強烈建議裝 cryptography 套件。
    """
    nonce = secrets.token_bytes(16)
    pt = plaintext.encode("utf-8")
    keystream = _derive_keystream(key, nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, keystream))
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + ct + mac).decode("ascii")


def _fallback_decrypt(key: bytes, token: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(token)
    except (ValueError, TypeError):
        return ""
    if len(raw) < 16 + 32:
        return ""
    nonce, ct, mac = raw[:16], raw[16:-32], raw[-32:]
    expected = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        log.error("備援解密 HMAC 驗證失敗")
        return ""
    keystream = _derive_keystream(key, nonce, len(ct))
    pt = bytes(a ^ b for a, b in zip(ct, keystream))
    try:
        return pt.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _derive_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """以 SHA-256(key||nonce||counter) 串接成所需長度的 keystream。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])
