"""
Клиент Flyer API (api.flyerhubs.com).
Документация: https://api.flyerhubs.com/redoc
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import FlyerConfig

logger = logging.getLogger(__name__)

SERVICE_SHUTDOWN_SEC = 5
CHECK_CACHE_TTL_SEC = 60


class FlyerAPIError(Exception):
    pass


class FlyerClient:
    """HTTP-клиент Flyer API."""

    def __init__(self, config: FlyerConfig):
        self.config = config
        self.api_url = config.api_url.rstrip("/")
        self.api_key = config.api_key
        self.enabled = config.enabled and bool(config.api_key)
        self._service_down_until = 0.0
        self._check_cache: dict[int, tuple[bool, float]] = {}
        self._client: httpx.AsyncClient | None = None

        if not config.enabled:
            logger.info("Flyer integration disabled (FLYER_ENABLED=false)")
            return
        if not config.api_key:
            logger.warning("Flyer enabled but FLYER_API_KEY is not set")
            return

        self._client = httpx.AsyncClient(
            base_url=self.api_url,
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(5.0),
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _service_unavailable(self) -> bool:
        return time.time() < self._service_down_until

    def _mark_service_down(self) -> None:
        self._service_down_until = time.time() + SERVICE_SHUTDOWN_SEC

    def _cache_get(self, user_id: int) -> bool | None:
        entry = self._check_cache.get(user_id)
        if not entry:
            return None
        subscribed, expires_at = entry
        if time.time() >= expires_at:
            self._check_cache.pop(user_id, None)
            return None
        return subscribed

    def _cache_set(self, user_id: int, subscribed: bool) -> None:
        if subscribed:
            self._check_cache[user_id] = (True, time.time() + CHECK_CACHE_TTL_SEC)

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._client or not self.api_key:
            raise FlyerAPIError("Flyer client is not configured")

        payload = {"key": self.api_key, **(params or {})}
        try:
            response = await self._client.post(f"/{method}", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.RequestError as e:
            self._mark_service_down()
            logger.error("Flyer %s request failed: %s", method, e)
            raise FlyerAPIError(str(e)) from e
        except httpx.HTTPStatusError as e:
            logger.error("Flyer %s HTTP error: %s", method, e)
            raise FlyerAPIError(str(e)) from e

        if data.get("error"):
            logger.error("Flyer %s error: %s", method, data["error"])
        elif data.get("warning"):
            logger.warning("Flyer %s warning: %s", method, data["warning"])
        elif data.get("info"):
            logger.info("Flyer %s info: %s", method, data["info"])

        return data

    async def get_me(self) -> dict[str, Any]:
        """Информация о ключе бота."""
        return await self._request("get_me")

    async def check(
        self,
        user_id: int,
        language_code: str | None = None,
        message: dict[str, Any] | None = None,
    ) -> bool:
        """
        Проверка обязательной подписки.
        True — пользователь прошёл проверку (skip), False — нужно подписаться.
        """
        if not self.enabled:
            return True
        if user_id < 0:
            return True
        if self._service_unavailable():
            return True

        cached = self._cache_get(user_id)
        if cached is True:
            return True

        params: dict[str, Any] = {"user_id": user_id}
        if language_code:
            params["language_code"] = language_code
        if message:
            params["message"] = message

        try:
            result = await self._request("check", params)
        except FlyerAPIError:
            return True

        if "skip" not in result and result.get("error"):
            raise FlyerAPIError(result["error"])

        subscribed = bool(result.get("skip"))
        if subscribed and "error" not in result:
            self._cache_set(user_id, True)
        return subscribed

    async def get_tasks(
        self,
        user_id: int,
        language_code: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Задания для пользователя (каналы, боты, ссылки)."""
        if not self.enabled or user_id < 0 or self._service_unavailable():
            return []

        params: dict[str, Any] = {"user_id": user_id}
        if language_code:
            params["language_code"] = language_code
        if limit is not None:
            params["limit"] = limit

        try:
            result = await self._request("get_tasks", params)
        except FlyerAPIError:
            return []

        if "result" not in result and result.get("error"):
            raise FlyerAPIError(result["error"])
        tasks = result.get("result")
        return tasks if isinstance(tasks, list) else []

    async def check_task(self, signature: str) -> str | None:
        """Статус задания: complete, incomplete, waiting, unavailable, abort."""
        if not self.enabled:
            return None

        try:
            result = await self._request("check_task", {"signature": signature})
        except FlyerAPIError:
            return None

        if "result" not in result and result.get("error"):
            raise FlyerAPIError(result["error"])
        return result.get("result")

    async def get_completed_tasks(self, user_id: int) -> dict[str, Any] | None:
        """Выполненные задания пользователя."""
        if not self.enabled or user_id < 0 or self._service_unavailable():
            return None

        try:
            result = await self._request("get_completed_tasks", {"user_id": user_id})
        except FlyerAPIError:
            return None

        if "result" not in result and result.get("error"):
            raise FlyerAPIError(result["error"])
        data = result.get("result")
        return data if isinstance(data, dict) else None
