from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx

from .config import XUIConfig
from .vless_link_builder import (
    build_vless_link,
    client_flow_for_network,
    resolve_listen_ip,
)


def _parse_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


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

    async def _post_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json", **self._auth_headers()}
        if not self.api_token:
            csrf_token = await self._fetch_csrf_token()
            if csrf_token:
                headers["X-CSRF-Token"] = csrf_token
        return headers

    async def _fetch_csrf_token(self) -> str | None:
        """3x-ui v3.3+ отдаёт CSRF-токен; на старых панелях endpoint отсутствует."""
        await self._client.get("")
        resp = await self._client.get("csrf-token")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"x-ui csrf-token failed: {resp.status_code} {resp.text}")
        data = resp.json()
        token = data.get("obj") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError(f"x-ui csrf-token missing in response: {resp.text}")
        return str(token)

    async def login(self) -> None:
        if self.api_token:
            self._authorized = True
            return
        if not (self.username and self.password):
            raise RuntimeError("Either API_TOKEN or USERNAME/PASSWORD must be provided")
        try:
            csrf_token = await self._fetch_csrf_token()
            if csrf_token:
                resp = await self._client.post(
                    "login",
                    json={"username": self.username, "password": self.password},
                    headers={"X-CSRF-Token": csrf_token},
                )
            else:
                resp = await self._client.post(
                    "login",
                    data={"username": self.username, "password": self.password},
                )
            if resp.status_code not in (200, 302):
                raise RuntimeError(f"x-ui login failed: {resp.status_code} {resp.text}")
            try:
                body = resp.json()
                if isinstance(body, dict) and body.get("success") is False:
                    raise RuntimeError(f"x-ui login failed: {body.get('msg') or body}")
            except Exception:  # noqa: BLE001
                pass
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

        inbound_id = self.inbound_id
        if not inbound_id:
            raise RuntimeError("inbound_id is not set")

        inbounds_resp = await self._client.get("panel/api/inbounds/list")
        inbounds = inbounds_resp.json().get("obj", [])
        chosen = next((i for i in inbounds if i.get("id") == inbound_id), None)
        if not chosen:
            raise RuntimeError("Inbound not found for client creation")

        stream_settings = _parse_json_field(chosen.get("streamSettings"))
        network = (stream_settings.get("network") or "tcp").lower()

        client_uuid = uuid.uuid4().hex
        email = f"tg_{telegram_user_id}_{int(time.time())}@xui"
        total_gb_bytes = 0 if not traffic_gb else traffic_gb * 1073741824

        client_dict: dict[str, Any] = {
            "id": client_uuid,
            "email": email,
            "alterId": 64,
            "limitIp": 3,
            "totalGB": total_gb_bytes,
            "expiryTime": expiry_time_unix_ms,
            "enable": True,
            "tgId": email,
            "subId": "",
        }
        flow = client_flow_for_network(network)
        if flow:
            client_dict["flow"] = flow

        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client_dict]}),
        }
        headers = await self._post_headers()

        resp = await self._client.post("panel/api/inbounds/addClient", json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"addClient failed: {resp.status_code} {resp.text}")

        data = resp.json()
        if not data.get("success", True):
            raise RuntimeError(f"addClient error: {data}")

        port = chosen.get("port") or "443"
        listen_ip = resolve_listen_ip(
            chosen_inbound=chosen,
            public_ip=public_ip,
            base_url=self.base_url,
        )
        if listen_ip in ("127.0.0.1", "localhost"):
            import logging
            logging.warning(
                "VLESS link generated with %s. This might not work for external clients.",
                listen_ip,
            )

        link = build_vless_link(
            client_uuid=client_uuid,
            listen_ip=listen_ip,
            port=port,
            stream_settings=stream_settings,
            display_name=display_name or email.split("@")[0],
        )

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
        network = (_parse_json_field(chosen.get("streamSettings")).get("network") or "tcp")
        expected_flow = client_flow_for_network(str(network).lower())
        for client in clients:
            if client.get("id") == client_id:
                client["expiryTime"] = expiry_time_unix_ms
                client["enable"] = True
                if expected_flow:
                    client["flow"] = expected_flow
                else:
                    client.pop("flow", None)
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

        headers = await self._post_headers()
        resp = await self._client.post(
            f"panel/api/inbounds/update/{inbound_id}",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to update client expiry: {resp.status_code} {resp.text}")

    async def delete_client(self, client_id: str) -> None:
        """Удалить клиента с панели (3x-ui)."""
        await self.ensure_login()
        inbound_id = self.inbound_id
        if not inbound_id:
            raise RuntimeError("inbound_id is not set")

        headers = await self._post_headers()
        resp = await self._client.post(
            f"panel/api/inbounds/{inbound_id}/delClient/{client_id}",
            headers=headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            if not (isinstance(data, dict) and data.get("success") is False):
                return

        # Fallback для других версий панели
        headers = await self._post_headers()
        resp = await self._client.post(
            "panel/api/inbounds/delClient",
            json={"id": inbound_id, "clientId": client_id},
            headers=headers,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"delClient failed: {resp.status_code} {resp.text}")
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"delClient error: {data}")

    async def close(self) -> None:
        """Закрыть клиент."""
        await self._client.aclose()


