"""OpenAI Chat Completions with tool calling."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .backend_service import TOOL_DEFINITIONS_READONLY, TOOL_DEFINITIONS_STAFF, execute_tool
from .config import SupportBotConfig

logger = logging.getLogger(__name__)

USER_SYSTEM = """Ты — вежливый ассистент техподдержки сервиса {service}.
Помогаешь с VPN, подпиской, оплатой, подключением, трафиком и приложениями.
У тебя есть инструменты для проверки данных пользователя в backend (профиль, логи активности, статус сервиса).
Текущий пользователь тикета: user_id={user_id}, username=@{username}, имя={first_name}.

Правила:
- Отвечай по-русски, кратко и по делу.
- Перед советами по подписке проверяй профиль через инструменты, если нужны факты.
- Не выдумывай данные — только из инструментов или общих знаний о VPN.
- Не раскрывай внутренние API-ключи, пароли, токены подписки целиком.
- Если не можешь решить — предложи нажать «Позвать человека».
- Не выполняй действия, меняющие подписку/блокировки — это только для операторов."""

STAFF_SYSTEM = """Ты — AI-помощник оператора SvoyVPN ({service}).
Полный доступ к backend как в /admin: пользователи, подписки, трафик bypass/месячный, серверы, цены, UTM, баланс, выводы, eSIM beta, логи.

КРИТИЧНО:
- Изменения — ТОЛЬКО через tools. Без успешного tool — нельзя писать «готово».
- «300 ГБ» на Pro = bypass_traffic_limit_gb (set_bypass_traffic_limit_gb), не traffic_limit_gb.
- После write смотри after/ok в JSON ответа инструмента — перескажи факты из БД.
- Сначала get_user_profile при работе с конкретным user_id.

Группы tools: профиль/поиск, подписка и tier, трафик, баланс, серверы, настройки, UTM, eSIM.

Уведомления (основной бот):
- get_notification_button_catalog — типы кнопок (menu с custom text, url, tier_pay, personal_promo)
- get_user_payment_context — рекуррент / продление перед скидкой
- send_user_notification — текст + buttons[]
- send_discount_notification — скидка N% + кнопка оплаты (учитывает автоплатёж)
- create_personal_discount_offer — только оффер без отправки"""

CSAT_SYSTEM = """Пользователь только что закрыл тикет. Кратко (1-2 предложения) поблагодари и попроси оценить работу поддержки по шкале 1-5."""


class SupportAIService:
    def __init__(self, config: SupportBotConfig):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={
                "Authorization": f"Bearer {config.openai_api_key}",
                "Content-Type": "application/json",
            },
            timeout=90.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _tools(self, staff: bool) -> list[dict]:
        return TOOL_DEFINITIONS_STAFF if staff else TOOL_DEFINITIONS_READONLY

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        staff: bool,
        max_tool_rounds: int = 5,
    ) -> str:
        tools = self._tools(staff)
        for _ in range(max_tool_rounds):
            payload: dict[str, Any] = {
                "model": self.config.openai_model,
                "messages": messages,
                "temperature": 0.4,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            resp = await self._client.post("/chat/completions", json=payload)
            if resp.status_code != 200:
                logger.error("OpenAI %s: %s", resp.status_code, resp.text[:500])
                return "Извините, ассистент временно недоступен. Нажмите «Позвать человека»."

            data = resp.json()
            choice = data["choices"][0]["message"]
            tool_calls = choice.get("tool_calls")

            if not tool_calls:
                content = (choice.get("content") or "").strip()
                return content or "Не удалось сформировать ответ."

            messages.append(choice)
            for tc in tool_calls:
                fn = tc["function"]
                args = json.loads(fn.get("arguments") or "{}")
                tool_result = await execute_tool(fn["name"], args, staff=staff)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    }
                )

        return "Запрос слишком сложный. Попробуйте уточнить или позовите оператора."

    async def reply_in_ticket(
        self,
        *,
        user_id: int,
        username: str | None,
        first_name: str | None,
        history: list[dict[str, str]],
        user_message: str,
    ) -> str:
        system = USER_SYSTEM.format(
            service=self.config.service_name,
            user_id=user_id,
            username=username or "—",
            first_name=first_name or "Пользователь",
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for h in history[-20:]:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": user_message})
        return await self._chat(messages, staff=False)

    async def staff_command(self, text: str, staff_id: int) -> str:
        system = STAFF_SYSTEM.format(service=self.config.service_name)
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"[Оператор {staff_id}]\n{text}",
            },
        ]
        return await self._chat(messages, staff=True)

    async def closing_message(self) -> str:
        messages = [
            {"role": "system", "content": CSAT_SYSTEM},
            {"role": "user", "content": "Тикет закрыт."},
        ]
        payload = {
            "model": self.config.openai_model,
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 200,
        }
        resp = await self._client.post("/chat/completions", json=payload)
        if resp.status_code == 200:
            return (resp.json()["choices"][0]["message"].get("content") or "").strip()
        return (
            "Спасибо, что обратились в поддержку! "
            "Оцените, пожалуйста, работу оператора от 1 до 5 (можно с коротким комментарием)."
        )
