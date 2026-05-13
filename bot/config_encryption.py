"""
Шифрование конфигов для Happ (crypt5 формат).

Схема:
1. Генерируем случайный AES-256 ключ и IV
2. Шифруем JSON профиль AES-256-GCM
3. Кодируем результат в base64url
4. Формируем crypt5 payload: version.iv.ciphertext.tag
5. Генерируем deep link: happ://crypt5/{payload}

Ключ шифрования привязан к subscription token пользователя,
что делает невозможным расшифровку без знания токена.

Подготовка к будущему:
- device binding (ключ = token + device_fingerprint)
- anti-share (одноразовые ключи)
- provider verification (подпись сервера)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


CRYPT5_VERSION = "5"

_ENCRYPTION_PEPPER = os.getenv(
    "SVOYVPN_CRYPT_PEPPER",
    "SvoyVPN-crypt5-default-pepper-2024",
)


def _derive_key(subscription_token: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    """
    Выводит AES-256 ключ из subscription_token + pepper.
    Возвращает (key, salt).
    """
    if salt is None:
        salt = os.urandom(16)
    material = f"{subscription_token}:{_ENCRYPTION_PEPPER}".encode()
    key = hashlib.pbkdf2_hmac("sha256", material, salt, iterations=100_000, dklen=32)
    return key, salt


def encrypt_profile(
    profile_json: str,
    subscription_token: str,
    *,
    hide_servers: bool = True,
) -> str:
    """
    Шифрует JSON профиль в crypt5 формат.

    Возвращает строку: {version}.{salt_b64}.{nonce_b64}.{ciphertext_b64}
    """
    key, salt = _derive_key(subscription_token)

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)

    aad = f"svoyvpn:crypt5:{subscription_token[:8]}".encode()
    plaintext = profile_json.encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, aad)

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    payload = ".".join([
        CRYPT5_VERSION,
        b64url(salt),
        b64url(nonce),
        b64url(ciphertext),
    ])
    return payload


def decrypt_profile(
    crypt5_payload: str,
    subscription_token: str,
) -> str:
    """
    Расшифровывает crypt5 payload обратно в JSON.
    Используется для тестирования и отладки.
    """
    parts = crypt5_payload.split(".")
    if len(parts) != 4 or parts[0] != CRYPT5_VERSION:
        raise ValueError(f"Invalid crypt5 payload (version={parts[0] if parts else '?'})")

    def b64url_decode(s: str) -> bytes:
        padding = 4 - len(s) % 4
        if padding != 4:
            s += "=" * padding
        return base64.urlsafe_b64decode(s)

    salt = b64url_decode(parts[1])
    nonce = b64url_decode(parts[2])
    ciphertext = b64url_decode(parts[3])

    key, _ = _derive_key(subscription_token, salt=salt)
    aesgcm = AESGCM(key)

    aad = f"svoyvpn:crypt5:{subscription_token[:8]}".encode()
    plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
    return plaintext.decode("utf-8")


def build_crypt5_deeplink(
    crypt5_payload: str,
    app: str = "happ",
) -> str:
    """Формирует deep link для импорта зашифрованного профиля."""
    return f"{app}://crypt5/{crypt5_payload}"


def build_encrypted_subscription_url(
    profile_json: str,
    subscription_token: str,
    *,
    base_url: str = "https://xdoublegroup.online",
) -> str:
    """
    Создаёт URL для получения зашифрованного профиля.
    Профиль отдаётся с сервера, а не встраивается в deep link.
    """
    return f"{base_url}/sub/{subscription_token}?format=crypt5"


# ---------------------------------------------------------------------------
# Profile sanitization (скрытие чувствительных данных для display mode)
# ---------------------------------------------------------------------------

def sanitize_profile_for_display(profile: dict[str, Any]) -> dict[str, Any]:
    """
    Очищает профиль от чувствительных данных для отображения / «безопасного» экспорта.
    Не подходит как рабочий конфиг — только для UI-превью.
    """
    import copy
    safe = copy.deepcopy(profile)

    for ob in safe.get("outbounds", []):
        ob_type = ob.get("type", ob.get("protocol", ""))

        if ob_type == "vless":
            if "server" in ob:
                ob["server"] = "***"
                ob["server_port"] = 0
                ob["uuid"] = "***"
                tls = ob.get("tls", {})
                tls.pop("server_name", None)
                reality = tls.get("reality", {})
                reality.pop("public_key", None)
                reality.pop("short_id", None)
            for vnext in ob.get("settings", {}).get("vnext", []):
                vnext["address"] = "***"
                vnext["port"] = 0
                for user in vnext.get("users", []):
                    user["id"] = "***"
            if "streamSettings" in ob:
                ob["streamSettings"] = {"info": "hidden"}
        else:
            if ob.get("protocol") in ("freedom", "blackhole", "dns"):
                continue
            ob.pop("settings", None)

    for key in ("routing", "route"):
        if key in safe and isinstance(safe[key], dict):
            safe[key] = {"info": "routing hidden"}

    if "dns" in safe:
        safe["dns"] = {"info": "hidden"}

    if "burstObservatory" in safe:
        safe["burstObservatory"] = {"info": "hidden"}

    if "policy" in safe:
        safe["policy"] = {"info": "hidden"}

    if "inbounds" in safe:
        safe["inbounds"] = [{"info": "hidden"}]

    return safe


# ---------------------------------------------------------------------------
# Future: Anti-share stubs
# ---------------------------------------------------------------------------

class AntiShareConfig:
    """
    Stub для будущей реализации anti-share.

    Планируется:
    - device_binding: ключ = token + device_fingerprint
    - one_time_keys: одноразовые ключи для каждого запроса
    - provider_verification: подпись сервера
    - max_devices: лимит устройств на уровне шифрования
    """

    def __init__(
        self,
        *,
        enable_device_binding: bool = False,
        enable_one_time_keys: bool = False,
        max_devices: int = 3,
    ):
        self.enable_device_binding = enable_device_binding
        self.enable_one_time_keys = enable_one_time_keys
        self.max_devices = max_devices

    def derive_device_key(
        self,
        subscription_token: str,
        device_fingerprint: str,
    ) -> str:
        """Stub: в будущем — ключ привязанный к устройству."""
        material = f"{subscription_token}:{device_fingerprint}:{_ENCRYPTION_PEPPER}"
        return hashlib.sha256(material.encode()).hexdigest()

    def generate_one_time_token(self, subscription_token: str) -> str:
        """Stub: одноразовый токен для запроса конфига."""
        ts = str(int(time.time()))
        nonce = os.urandom(8).hex()
        material = f"{subscription_token}:{ts}:{nonce}"
        sig = hashlib.sha256(f"{material}:{_ENCRYPTION_PEPPER}".encode()).hexdigest()[:16]
        return f"{ts}.{nonce}.{sig}"
