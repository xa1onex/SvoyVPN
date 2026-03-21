from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx

from .config import XUIConfig


class XUIClient:
    """Минимальный клиент для работы с x-ui/3x-ui (создание и продление клиентов)."""

    def __init__(
        self,
        cfg: XUIConfig | None = None,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        api_token: str | None = None,
        inbound_id: int | None = None,
    ) -> None:
        if cfg:
            self.base_url = cfg.base_url.rstrip("/") + "/"
            self.username = cfg.username
            self.password = cfg.password
            self.api_token = cfg.api_token
            self.inbound_id = getattr(cfg, "inbound_id", inbound_id)
        else:
            if not base_url:
                raise ValueError("Either cfg or base_url must be provided")
            self.base_url = base_url.rstrip("/") + "/"
            self.username = username
            self.password = password
            self.api_token = api_token
            self.inbound_id = inbound_id

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=20.0,
            verify=False,  # TODO: включить verify и настроить сертификаты
            follow_redirects=True,
        )
        self._authorized = False

    def _auth_headers(self) -> dict[str, str]:
        if self.api_token:
            return {"Authorization": f"Bearer {self.api_token}"}
        return {}

    async def login(self) -> None:
        if self.api_token:
            self._authorized = True
            return
        if not (self.username and self.password):
            raise RuntimeError("Either API_TOKEN or USERNAME/PASSWORD must be provided")
        try:
            resp = await self._client.post(
                "login",
                data={"username": self.username, "password": self.password},
            )
            if resp.status_code not in (200, 302):
                raise RuntimeError(f"x-ui login failed: {resp.status_code} {resp.text}")
            self._authorized = True
        except httpx.ConnectError as e:
            raise RuntimeError(f"Connection error: {e}") from e
        except httpx.TimeoutException as e:
            raise RuntimeError(f"Connection timeout: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Login error: {e}") from e

    async def ensure_login(self) -> None:
        if not self._authorized:
            await self.login()

    async def add_vless_client(
        self,
        telegram_user_id: int,
        display_name: str,
        traffic_gb: int | None,
        expiry_time_unix_ms: int,
        public_ip: str | None = None,
    ) -> dict[str, Any]:
        """Создать VLESS-клиента с заданным сроком."""
        await self.ensure_login()

        client_uuid = uuid.uuid4().hex
        email = f"tg_{telegram_user_id}_{int(time.time())}@xui"
        total_gb_bytes = 0 if not traffic_gb else traffic_gb * 1073741824

        client_dict = {
            "id": client_uuid,
            "email": email,
            "alterId": 64,
            "limitIp": 3,
            "totalGB": total_gb_bytes,
            "expiryTime": expiry_time_unix_ms,
            "enable": True,
            "tgId": email,
            "subId": "",
            "flow": "xtls-rprx-vision",
        }

        inbound_id = self.inbound_id
        if not inbound_id:
            raise RuntimeError("inbound_id is not set")

        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client_dict]}),
        }
        headers = {"Content-Type": "application/json", **self._auth_headers()}

        resp = await self._client.post("panel/api/inbounds/addClient", json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"addClient failed: {resp.status_code} {resp.text}")

        data = resp.json()
        if not data.get("success", True):
            raise RuntimeError(f"addClient error: {data}")

        # Получаем inbound, чтобы построить ссылку
        inbounds_resp = await self._client.get("panel/api/inbounds/list")
        inbounds = inbounds_resp.json().get("obj", [])
        chosen = next((i for i in inbounds if i.get("id") == inbound_id), None)
        if not chosen:
            raise RuntimeError("Inbound not found for link generation")

        port = chosen.get("port") or "443"
        stream_settings = json.loads(chosen.get("streamSettings", "{}") or "{}")
        reality_settings = stream_settings.get("realitySettings") or {}

        pbk = ""
        sid = ""
        sni = "google.com"
        fp = "chrome"

        if reality_settings:
            settings = reality_settings.get("settings", {})
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except Exception:  # noqa: BLE001
                    settings = {}
            elif not isinstance(settings, dict):
                settings = {}

            pbk = settings.get("publicKey", "") or ""

            sid = reality_settings.get("shortId", "") or ""
            if not sid:
                short_ids = reality_settings.get("shortIds", []) or settings.get("shortIds", [])
                if isinstance(short_ids, list) and short_ids:
                    sid = short_ids[0]
                elif isinstance(short_ids, str):
                    sid = short_ids

            sni_list = reality_settings.get("serverNames", [])
            if isinstance(sni_list, str):
                try:
                    sni_list = json.loads(sni_list)
                except Exception:  # noqa: BLE001
                    sni_list = [sni_list] if sni_list else []
            if isinstance(sni_list, list) and sni_list:
                sni = sni_list[0]
            elif isinstance(sni_list, str) and sni_list:
                sni = sni_list

            fingerprints = settings.get("fingerprints", []) or reality_settings.get(
                "fingerprints",
                [],
            )
            if isinstance(fingerprints, str):
                try:
                    fingerprints = json.loads(fingerprints)
                except Exception:  # noqa: BLE001
                    fingerprints = [fingerprints] if fingerprints else []
            if isinstance(fingerprints, list) and fingerprints:
                fp = fingerprints[0]
            elif isinstance(fingerprints, str) and fingerprints:
                fp = fingerprints

        # Определяем IP для ссылки
        listen_ip = public_ip
        if not listen_ip:
            listen_ip = chosen.get("listen") or ""
            if not listen_ip or listen_ip in ("0.0.0.0", "127.0.0.1", "localhost"):
                url_part = self.base_url.split("//")[-1].split("/")[0]
                listen_ip = url_part.split(":")[0]
                
        # Если все еще 127.0.0.1 — это проблема для внешнего клиента
        if listen_ip in ("127.0.0.1", "localhost"):
             import logging
             logging.warning(f"VLESS link generated with {listen_ip}. This might not work for external clients.")

        link = f"vless://{client_uuid}@{listen_ip}:{port}/?type=tcp&encryption=none&security=reality"
        if pbk:
            link += f"&pbk={pbk}"
        link += f"&fp={fp}"
        link += f"&sni={sni}"
        link += f"&sid={sid or '3d'}"
        link += "&spx=%2F&flow=xtls-rprx-vision"
        link += f"#{display_name or email.split('@')[0]}"

        return {
            "email": email,
            "id": client_uuid,
            "expires_ms": expiry_time_unix_ms,
            "traffic_gb": traffic_gb or 0,
            "link": link,
        }

    async def update_client_expiry(self, client_id: str, expiry_time_unix_ms: int) -> None:
        """Продлить срок клиента на панели."""
        await self.ensure_login()
        inbound_id = self.inbound_id
        if not inbound_id:
            raise RuntimeError("inbound_id is not set")

        inbounds_resp = await self._client.get("panel/api/inbounds/list")
        inbounds = inbounds_resp.json().get("obj", [])
        chosen = next((i for i in inbounds if i.get("id") == inbound_id), None)
        if not chosen:
            raise RuntimeError(f"Inbound {inbound_id} not found")

        settings_str = chosen.get("settings", "{}")
        try:
            settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
        except Exception:  # noqa: BLE001
            settings = {}

        clients = settings.get("clients", [])
        if not isinstance(clients, list):
            clients = []

        client_found = False
        for client in clients:
            if client.get("id") == client_id:
                client["expiryTime"] = expiry_time_unix_ms
                client_found = True
                break

        if not client_found:
            raise RuntimeError(f"Client {client_id} not found in inbound")

        settings["clients"] = clients
        updated_settings = json.dumps(settings)

        required_fields = [
            "id",
            "settings",
            "streamSettings",
            "sniffing",
            "protocol",
            "port",
            "listen",
            "remark",
            "enable",
            "expiryTime",
            "trafficReset",
            "lastTrafficResetTime",
            "tag",
        ]
        payload: dict[str, Any] = {}
        for key in required_fields:
            if key in chosen:
                payload[key] = updated_settings if key == "settings" else chosen[key]

        headers = {"Content-Type": "application/json", **self._auth_headers()}
        resp = await self._client.post(
            f"panel/api/inbounds/update/{inbound_id}",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to update client expiry: {resp.status_code} {resp.text}")

    async def close(self) -> None:
        """Закрыть клиент."""
        await self._client.aclose()


