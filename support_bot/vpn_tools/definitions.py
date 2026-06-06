"""OpenAI function schemas for VPN bot backend."""

from __future__ import annotations


def _fn(name: str, description: str, props: dict, required: list | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required or list(props.keys()),
            },
        },
    }


_I = {"type": "integer"}
_S = {"type": "string"}
_B = {"type": "boolean"}
_N = {"type": "number"}

READ_TOOLS = [
    _fn("get_user_profile", "Карточка пользователя: подписка, трафик bypass/месячный, платежи", {"user_id": {**_I, "description": "Telegram ID"}}),
    _fn("search_user", "Поиск по username или user_id", {"query": {**_S, "description": "username или ID"}}),
    _fn("get_user_activity", "Логи /sub и miniapp, IP подписки", {"user_id": _I, "limit": {**_I, "description": "1-30, default 15"}}, ["user_id"]),
    _fn("get_user_payments", "История платежей пользователя", {"user_id": _I, "limit": {**_I, "description": "1-30"}}, ["user_id"]),
    _fn("get_user_vpn_keys", "Активные VPN-ключи пользователя по серверам", {"user_id": _I}),
    _fn("get_user_balance", "Реферальный баланс и последние транзакции", {"user_id": _I}),
    _fn("get_user_devices", "Активные устройства за последние 6ч vs лимит", {"user_id": _I}),
    _fn("get_service_overview", "Сводка: юзеры, подписки, серверы, платежи сегодня", {}),
    _fn("get_admin_stats", "Расширенная статистика как в /admin панели", {}),
    _fn("get_system_logs", "journalctl основного сервиса", {"lines": {**_I, "description": "10-150"}}, []),
    _fn("list_servers", "Список VPN-серверов", {"active_only": {**_B, "description": "только активные"}}),
    _fn("get_server_detail", "Детали сервера и число ключей", {"server_id": _I}),
    _fn("get_traffic_settings", "Глобальные настройки трафика (default GB, sync)", {}),
    _fn("get_pricing_catalog", "Тарифы Lite/Standard/Pro и legacy цены из БД", {}),
    _fn("get_bypass_packs", "Каталог bypass-пакетов ГБ", {}),
    _fn("get_gb_traffic_packs", "Каталог месячных GB-пакетов", {}),
    _fn("get_referral_settings", "Бонусные дни рефералки", {}),
    _fn("get_trial_settings", "Длительность триала в днях", {}),
    _fn("get_discount_settings", "Настройки скидок", {}),
    _fn("get_announcement", "Текст объявления в главном меню бота", {}),
    _fn("list_support_managers", "Менеджеры техподдержки", {}),
    _fn("list_utm_campaigns", "UTM-кампании и статистика", {"limit": {**_I, "description": "1-50"}}, []),
    _fn("list_pending_withdrawals", "Заявки на вывод реф. баланса (pending)", {}),
    _fn("get_global_activity_logs", "Последние логи VPN/miniapp всех юзеров", {"limit": {**_I, "description": "1-50"}}, []),
    _fn("list_esim_beta_requests", "Заявки eSIM beta", {"status": {**_S, "description": "pending|approved|rejected|all"}}, []),
    _fn("get_notification_button_catalog", "Справочник кнопок для уведомлений (меню, callback, url, скидки)", {}),
    _fn("get_user_payment_context", "Рекуррентная карта, продление, тариф — перед скидочным оффером", {"user_id": _I}),
]

NOTIFY_BUTTON_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "description": "menu|callback|url|tier_pay|personal_promo|builtin_promo",
        },
        "text": {**_S, "description": "Текст на кнопке (свой)"},
        "menu_key": _S,
        "callback_data": _S,
        "url": _S,
        "plan_id": _S,
        "offer_id": _I,
        "promo_key": _S,
    },
}

WRITE_TOOLS = [
    _fn("extend_user_subscription", "Продлить подписку на N месяцев от даты окончания", {"user_id": _I, "months": {**_I, "description": "1-12"}}),
    _fn("extend_user_subscription_days", "Продлить подписку на N дней", {"user_id": _I, "days": {**_I, "description": "1-365"}}),
    _fn("grant_subscription_days", "Подписка N дней с сегодня (перезапись)", {"user_id": _I, "days": {**_I, "description": "1-365"}}),
    _fn("set_subscription_tier", "Тариф: free|lite|standard|pro|legacy + лимиты тарифа", {"user_id": _I, "tier": {**_S, "description": "free/lite/standard/pro/legacy"}}),
    _fn("grant_free_tier", "Перевести на бесплатный Free тариф", {"user_id": _I}),
    _fn("set_device_limit", "Лимит устройств", {"user_id": _I, "device_limit": {**_I, "description": "1-20"}}),
    _fn("reset_user_trial", "Сбросить trial_used=false", {"user_id": _I}),
    _fn("sync_user_vpn_keys", "Синхронизировать ключи с панелью X-UI", {"user_id": _I}),
    _fn("provision_user_keys", "Создать/активировать ключи на всех серверах", {"user_id": _I}),
    _fn("set_user_blacklist", "Блокировка пользователя", {"user_id": _I, "blacklisted": _B}),
    _fn("set_bypass_traffic_limit_gb", "Лимит bypass ГБ (Pro 300 и т.д.)", {"user_id": _I, "limit_gb": _I}),
    _fn("add_bypass_traffic_bonus_gb", "Бонус bypass ГБ", {"user_id": _I, "bonus_gb": _I}),
    _fn("set_monthly_traffic_limit_gb", "Персональный месячный лимит ГБ", {"user_id": _I, "limit_gb": _I}),
    _fn("add_monthly_traffic_bonus_gb", "Бонус к месячному лимиту", {"user_id": _I, "bonus_gb": _I}),
    _fn("adjust_user_balance", "Изменить реф. баланс в копейках (+ пополнение, - списание)", {"user_id": _I, "amount_cents": {**_I, "description": "копейки, + или -"}, "reason": {**_S, "description": "комментарий"}}),
    _fn("approve_withdrawal", "Одобрить заявку на вывод", {"request_id": _I}),
    _fn("reject_withdrawal", "Отклонить заявку, вернуть на баланс", {"request_id": _I}),
    _fn("toggle_server_active", "Вкл/выкл сервер", {"server_id": _I, "active": _B}),
    _fn("toggle_server_bypass", "Флаг bypass-сервера", {"server_id": _I, "is_bypass": _B}),
    _fn("toggle_server_exclude_subscription", "Скрыть сервер из подписки", {"server_id": _I, "exclude": _B}),
    _fn("set_global_traffic_default_gb", "Дефолт месячного лимита для всех", {"default_gb": _I}),
    _fn("set_trial_days", "Дней триала для новых", {"days": _I}),
    _fn("set_referral_bonus_days", "Бонусные дни рефералки", {"inviter_days": _I, "invited_days": _I}),
    _fn("set_tier_price", "Цена тарифа", {"tier": _S, "duration_months": _I, "price_rub": {**_I, "description": "копейки"}, "price_stars": _I}, ["tier", "duration_months"]),
    _fn("set_legacy_plan_price", "Цена legacy плана", {"plan_id": _S, "price_rub": _I, "price_stars": _I}, ["plan_id"]),
    _fn("create_utm_campaign", "Создать UTM", {"tag": _S, "description": _S, "bonus_days": _I}, ["tag"]),
    _fn("toggle_utm_campaign", "Вкл/выкл UTM", {"tag": _S, "active": _B}),
    _fn("upsert_support_manager", "Добавить менеджера ТП", {"user_id": _I, "support_link": _S}),
    _fn("deactivate_support_manager", "Отключить менеджера", {"user_id": _I}),
    _fn("set_announcement_text", "Текст объявления в меню", {"text": _S}),
    _fn("resolve_esim_beta", "Одобрить/отклонить eSIM beta", {"request_id": _I, "approve": _B}),
    _fn(
        "send_user_notification",
        "Отправить личное уведомление через ОСНОВНОЙ бот (HTML). Кнопки — menu/callback/url/tier_pay/personal_promo",
        {
            "user_id": _I,
            "text": {**_S, "description": "Текст сообщения HTML"},
            "buttons": {
                "type": "array",
                "items": NOTIFY_BUTTON_SCHEMA,
                "description": "Кнопки под сообщением",
            },
        },
    ),
    _fn(
        "create_personal_discount_offer",
        "Создать персональную скидку N% и вернуть offer_id + callback для кнопки. Учитывает рекуррент.",
        {
            "user_id": _I,
            "discount_percent": {**_I, "description": "1-99"},
            "tier": {**_S, "description": "lite|standard|pro, если нет plan_id"},
            "plan_id": {**_S, "description": "например standard_1m"},
            "button_text": {**_S, "description": "свой текст кнопки"},
            "note": _S,
            "valid_hours": {**_I, "description": "срок оффера, default 72"},
        },
        ["user_id", "discount_percent"],
    ),
    _fn(
        "send_discount_notification",
        "Создать скидочный оффер + отправить уведомление с кнопкой оплаты одним шагом",
        {
            "user_id": _I,
            "text": _S,
            "discount_percent": _I,
            "tier": _S,
            "plan_id": _S,
            "button_text": _S,
            "extra_buttons": {
                "type": "array",
                "items": NOTIFY_BUTTON_SCHEMA,
            },
        },
        ["user_id", "text", "discount_percent"],
    ),
]

TOOL_DEFINITIONS_READONLY = READ_TOOLS
TOOL_DEFINITIONS_STAFF = READ_TOOLS + WRITE_TOOLS

READ_TOOL_NAMES = frozenset(t["function"]["name"] for t in READ_TOOLS)
WRITE_TOOL_NAMES = frozenset(t["function"]["name"] for t in WRITE_TOOLS)
