from __future__ import annotations

import base64
import logging
import re
from datetime import date, datetime, time as dt_time
from typing import Any
from urllib.parse import unquote

import httpx

logger = logging.getLogger(__name__)


class RemnawaveClient:
    """Клиент Remnawave Panel API (https://docs.rw)."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        config_profile_uuid: str | None = None,
        inbound_uuid: str | None = None,
        internal_squad_uuid: str | None = None,
        grpc_path: str = "grpc.remnawave",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token.strip()
        self.config_profile_uuid = config_profile_uuid or "00000000-0000-0000-0000-000000000000"
        self.inbound_uuid = inbound_uuid or "b1ac2590-d0c3-4e58-bb62-4aae2280f69e"
        self.internal_squad_uuid = internal_squad_uuid or "b5f0d64c-ec52-4bd6-87c8-98495d63209c"
        self.grpc_path = grpc_path
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/",
            timeout=30.0,
            headers={"Authorization": f"Bearer {self.api_token}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._client.request(method, path.lstrip("/"), **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"Remnawave API {method} {path}: {resp.status_code} {resp.text}")
        data = resp.json()
        return data.get("response", data)

    async def list_nodes(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "nodes")
        if isinstance(data, list):
            return data
        return []

    async def find_connected_node(self, address: str) -> dict[str, Any] | None:
        address = address.strip().lower()
        for node in await self.list_nodes():
            if (node.get("address") or "").strip().lower() == address and node.get("isConnected"):
                return node
        return None

    async def create_host(
        self,
        *,
        remark: str,
        address: str,
        port: int,
        node_uuid: str,
        path: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "remark": remark,
            "address": address,
            "port": port,
            "path": path or self.grpc_path,
            "inbound": {
                "configProfileUuid": self.config_profile_uuid,
                "configProfileInboundUuid": self.inbound_uuid,
            },
            "nodes": [node_uuid],
        }
        return await self._request("POST", "hosts/", json=payload)

    async def get_user_by_telegram_id(self, telegram_id: int) -> dict[str, Any] | None:
        data = await self._request("GET", f"users/by-telegram-id/{telegram_id}")
        if isinstance(data, list):
            return data[0] if data else None
        return data or None

    async def create_user(
        self,
        *,
        telegram_id: int,
        expire_at: datetime,
        username: str | None = None,
        traffic_limit_bytes: int = 0,
    ) -> dict[str, Any]:
        payload = {
            "username": username or f"tg_{telegram_id}",
            "status": "ACTIVE",
            "trafficLimitBytes": traffic_limit_bytes,
            "trafficLimitStrategy": "NO_RESET",
            "expireAt": expire_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "telegramId": telegram_id,
            "activeInternalSquads": [self.internal_squad_uuid],
        }
        return await self._request("POST", "users/", json=payload)

    async def update_user(
        self,
        *,
        user_uuid: str,
        expire_at: datetime | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"uuid": user_uuid}
        if expire_at is not None:
            payload["expireAt"] = expire_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if status is not None:
            payload["status"] = status
        return await self._request("PATCH", "users/", json=payload)

    async def ensure_user(
        self,
        *,
        telegram_id: int,
        expire_at: datetime,
    ) -> dict[str, Any]:
        existing = await self.get_user_by_telegram_id(telegram_id)
        if existing:
            return await self.update_user(
                user_uuid=existing["uuid"],
                expire_at=expire_at,
                status="ACTIVE",
            )
        return await self.create_user(
            telegram_id=telegram_id,
            expire_at=expire_at,
        )

    async def fetch_subscription_links(self, short_uuid: str) -> list[str]:
        sub_url = f"{self.base_url}/api/sub/{short_uuid}"
        resp = await httpx.AsyncClient(timeout=20.0).get(sub_url)
        if resp.status_code >= 400:
            raise RuntimeError(f"Remnawave subscription fetch failed: {resp.status_code}")
        raw = resp.text.strip()
        if not raw:
            return []
        try:
            decoded = base64.b64decode(raw).decode("utf-8")
        except Exception:
            decoded = raw
        return [line.strip() for line in decoded.splitlines() if line.strip().startswith("vless://")]

    @staticmethod
    def extract_link_remark(vless_link: str) -> str:
        if "#" not in vless_link:
            return ""
        return unquote(vless_link.rsplit("#", 1)[-1])

    @staticmethod
    def normalize_host_remark(remark: str) -> str:
        """Сравнимый remark: без Happ-caption и маркетингового хвоста «| 🎉 NEW»."""
        r = (remark or "").strip()
        r = r.split("?", 1)[0].strip()
        if "|" in r:
            r = r.split("|", 1)[0].strip()
        return r

    @classmethod
    def remarks_match(cls, left: str, right: str) -> bool:
        a = (left or "").strip()
        b = (right or "").strip()
        if not a or not b:
            return False
        if a == b:
            return True
        return cls.normalize_host_remark(a) == cls.normalize_host_remark(b)

    async def get_vless_link_for_host_remark(self, short_uuid: str, host_remark: str) -> str | None:
        host_remark = host_remark.strip()
        links = await self.fetch_subscription_links(short_uuid)
        for link in links:
            remark = self.extract_link_remark(link)
            if self.remarks_match(remark, host_remark):
                return link
        # fallback: unique xhttp/bypass host if only one matches normalized prefix
        want = self.normalize_host_remark(host_remark)
        fuzzy = []
        for link in links:
            remark = self.normalize_host_remark(self.extract_link_remark(link))
            if want and (want in remark or remark in want):
                fuzzy.append(link)
        if len(fuzzy) == 1:
            return fuzzy[0]
        return None

    @staticmethod
    def parse_expiry_datetime(subscription_end: date | datetime | str) -> datetime:
        if isinstance(subscription_end, datetime):
            end_date = subscription_end.date()
        elif isinstance(subscription_end, date):
            end_date = subscription_end
        elif isinstance(subscription_end, str):
            end_date = datetime.strptime(
                subscription_end.split()[0], "%Y-%m-%d"
            ).date()
        else:
            raise ValueError(f"Unsupported subscription_end type: {type(subscription_end)}")
        return datetime.combine(end_date, dt_time(23, 59, 59))

    @staticmethod
    def extract_vless_uuid(vless_link: str) -> str:
        match = re.match(r"vless://([^@]+)@", vless_link)
        if not match:
            return ""
        return match.group(1)

    async def ping(self) -> bool:
        await self.list_nodes()
        return True

    @staticmethod
    def traffic_bytes_from_user(user: dict[str, Any] | None) -> int:
        """Lifetime трафик пользователя Remnawave (NO_RESET: used == lifetime)."""
        if not user:
            return 0
        traffic = user.get("userTraffic") or {}
        lifetime = int(traffic.get("lifetimeUsedTrafficBytes") or 0)
        used = int(traffic.get("usedTrafficBytes") or 0)
        return lifetime if lifetime > 0 else used

    async def fetch_all_users_traffic(self, *, page_size: int = 500) -> dict[str, int]:
        """
        Карта vlessUuid (без дефисов, lower) → lifetimeUsedTrafficBytes.
        Один пользователь Remnawave = один UUID на всех хостах панели.
        """
        usage: dict[str, int] = {}
        start = 0
        page_size = max(50, min(int(page_size), 500))
        while True:
            data = await self._request(
                "GET",
                "users",
                params={"size": page_size, "start": start},
            )
            if isinstance(data, list):
                users = data
                total = None
            else:
                users = data.get("users") or []
                total = data.get("total")
            if not users:
                break
            for user in users:
                if not isinstance(user, dict):
                    continue
                norm = str(user.get("vlessUuid") or "").strip().lower().replace("-", "")
                if not norm:
                    continue
                bytes_val = self.traffic_bytes_from_user(user)
                prev = usage.get(norm, 0)
                if bytes_val > prev:
                    usage[norm] = bytes_val
            if total is not None and start + len(users) >= int(total):
                break
            if len(users) < page_size:
                break
            start += len(users)
        return usage


def build_remnawave_client(config) -> RemnawaveClient:
    rw = config.remnawave
    if not rw.enabled or not rw.panel_url or not rw.api_token:
        raise RuntimeError(
            "Remnawave не настроен. Добавьте REMNAWAVE_ENABLED, REMNAWAVE_PANEL_URL и REMNAWAVE_API_TOKEN в .env"
        )
    return RemnawaveClient(
        base_url=rw.panel_url,
        api_token=rw.api_token,
        config_profile_uuid=rw.config_profile_uuid,
        inbound_uuid=rw.inbound_uuid,
        internal_squad_uuid=rw.internal_squad_uuid,
        grpc_path=rw.grpc_path,
    )


def is_remnawave_server(server: dict) -> bool:
    return (server.get("panel_type") or "3x-ui") == "remnawave"
