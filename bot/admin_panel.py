"""
Админ-панель: меню и актуальная статистика (Free / Plus).
"""
from __future__ import annotations

from datetime import datetime

import pytz
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .plans import (
    ALL_PAID_TIER_IDS,
    FREE_TIER_ID,
    LEGACY_TIER_IDS,
    PAID_TIER_IDS,
    SENTINEL_SUBSCRIPTION_END_THRESHOLD,
    format_price_rub,
)
from .activity_log import ACTION_LABELS


def get_admin_panel_keyboard():
    """Главное меню админки."""
    builder = InlineKeyboardBuilder()

    # Мониторинг
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
        InlineKeyboardButton(text="🔔 Логи", callback_data="admin_realtime_logs"),
    )
    builder.row(
        InlineKeyboardButton(text="👤 Пользователь", callback_data="admin_user_info"),
    )

    # Тарифы и продукт
    builder.row(
        InlineKeyboardButton(text="💎 Plus / bypass", callback_data="admin_tier_prices"),
        InlineKeyboardButton(text="📶 Трафик", callback_data="admin_traffic"),
    )
    builder.row(
        InlineKeyboardButton(text="🎁 Скидки", callback_data="admin_discounts"),
        InlineKeyboardButton(text="🆓 Пробный", callback_data="admin_trial"),
    )

    # Инфраструктура
    builder.row(
        InlineKeyboardButton(text="🖥️ Серверы", callback_data="admin_servers"),
        InlineKeyboardButton(text="📱 Happ", callback_data="admin_device_apps"),
    )

    # Маркетинг
    builder.row(
        InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"),
        InlineKeyboardButton(text="✏️ Объявление", callback_data="edit_announcement"),
    )
    builder.row(
        InlineKeyboardButton(text="🎁 Рефералы", callback_data="admin_referral"),
        InlineKeyboardButton(text="📈 UTM", callback_data="admin_utm"),
    )

    # Управление
    builder.row(
        InlineKeyboardButton(text="💳 Баланс", callback_data="admin_balance"),
        InlineKeyboardButton(text="👥 Админы", callback_data="admin_manage_admins"),
    )
    builder.row(
        InlineKeyboardButton(text="🛟 Менеджеры", callback_data="admin_manage_managers"),
    )

    builder.row(
        InlineKeyboardButton(text="◀️ В бот", callback_data="admin_back_to_main"),
    )
    return builder.as_markup()


async def build_admin_stats_text(conn) -> str:
    """Актуальная статистика под модель Free / Plus."""
    paid_tiers_sql = ", ".join(f"'{t}'" for t in ALL_PAID_TIER_IDS)

    total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
    new_today = await conn.fetchval(
        "SELECT COUNT(*) FROM users WHERE DATE(registration_date) = CURRENT_DATE"
    )
    new_week = await conn.fetchval(
        "SELECT COUNT(*) FROM users WHERE registration_date >= CURRENT_DATE - INTERVAL '7 days'"
    )

    free_users = await conn.fetchval(
        """
        SELECT COUNT(*) FROM users
        WHERE COALESCE(subscription_tier, $1) = $1
          AND pay_subscribed = TRUE
          AND blacklisted = FALSE
        """,
        FREE_TIER_ID,
    )

    plus_users = await conn.fetchval(
        f"""
        SELECT COUNT(*) FROM users
        WHERE subscription_tier IN ({", ".join(f"'{t}'" for t in PAID_TIER_IDS)})
          AND pay_subscribed = TRUE
          AND subscription_end IS NOT NULL
          AND DATE(subscription_end) >= CURRENT_DATE
          AND DATE(subscription_end) < $1
          AND blacklisted = FALSE
        """,
        SENTINEL_SUBSCRIPTION_END_THRESHOLD,
    )

    legacy_paid = 0
    if LEGACY_TIER_IDS:
        legacy_sql = ", ".join(f"'{t}'" for t in LEGACY_TIER_IDS)
        legacy_paid = await conn.fetchval(
            f"""
            SELECT COUNT(*) FROM users
            WHERE subscription_tier IN ({legacy_sql})
              AND pay_subscribed = TRUE
              AND subscription_end IS NOT NULL
              AND DATE(subscription_end) >= CURRENT_DATE
              AND DATE(subscription_end) < $1
              AND blacklisted = FALSE
            """,
            SENTINEL_SUBSCRIPTION_END_THRESHOLD,
        ) or 0

    with_card = await conn.fetchval(
        """
        SELECT COUNT(*) FROM users
        WHERE yookassa_recurring_payment_method_id IS NOT NULL
          AND blacklisted = FALSE
        """
    )

    in_grace = await conn.fetchval(
        """
        SELECT COUNT(*) FROM users
        WHERE autopay_grace_until IS NOT NULL
          AND DATE(autopay_grace_until) >= CURRENT_DATE
        """
    )

    active_7d = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM users
        WHERE last_activity >= CURRENT_DATE - INTERVAL '7 days'
        """
    )
    active_30d = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM users
        WHERE last_activity >= CURRENT_DATE - INTERVAL '30 days'
        """
    )
    inactive_30d = await conn.fetchval(
        """
        SELECT COUNT(*) FROM users
        WHERE last_activity < CURRENT_DATE - INTERVAL '30 days'
           OR last_activity IS NULL
        """
    )

    sub_dau = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM subscription_usage_logs
        WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
        """
    )
    sub_wau = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM subscription_usage_logs
        WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        """
    )
    sub_mau = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM subscription_usage_logs
        WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '30 days'
        """
    )
    sub_requests_today = await conn.fetchval(
        """
        SELECT COUNT(*) FROM subscription_usage_logs
        WHERE DATE(timestamp) = CURRENT_DATE
        """
    )

    total_revenue_rub = await conn.fetchval(
        """
        SELECT COALESCE(SUM(amount), 0) FROM payments
        WHERE currency = 'RUB' AND status = 'completed'
        """
    )
    revenue_30d_rub = await conn.fetchval(
        """
        SELECT COALESCE(SUM(amount), 0) FROM payments
        WHERE currency = 'RUB' AND status = 'completed'
          AND timestamp >= CURRENT_DATE - INTERVAL '30 days'
        """
    )
    revenue_today_rub = await conn.fetchval(
        """
        SELECT COALESCE(SUM(amount), 0) FROM payments
        WHERE currency = 'RUB' AND status = 'completed'
          AND DATE(timestamp) = CURRENT_DATE
        """
    )
    payments_today = await conn.fetchval(
        """
        SELECT COUNT(*) FROM payments
        WHERE status = 'completed' AND DATE(timestamp) = CURRENT_DATE
        """
    )
    paying_users = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM payments WHERE status = 'completed'
        """
    )
    paying_users_30d = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM payments
        WHERE status = 'completed'
          AND timestamp >= CURRENT_DATE - INTERVAL '30 days'
        """
    )

    arpu_30d = 0.0
    arppu_30d = 0.0
    if active_30d:
        arpu_30d = (revenue_30d_rub or 0) / 100.0 / active_30d
    if paying_users_30d:
        arppu_30d = (revenue_30d_rub or 0) / 100.0 / paying_users_30d

    revenue_by_type = await conn.fetch(
        """
        SELECT COALESCE(plan_type, 'other') AS pt,
               COUNT(*) AS cnt,
               COALESCE(SUM(amount), 0) AS total
        FROM payments
        WHERE status = 'completed' AND currency = 'RUB'
          AND timestamp >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY plan_type
        ORDER BY total DESC
        """
    )
    type_lines = ""
    type_labels = {
        "tier": "Plus подписка",
        "tier_upgrade": "Апгрейд",
        "bypass_pack": "Bypass ГБ",
        "gb_pack": "ГБ пакет",
        "subscription": "Legacy подписка",
        "renewal": "Legacy продление",
    }
    for row in revenue_by_type:
        label = type_labels.get(row["pt"], row["pt"])
        type_lines += f"  • {label}: {row['cnt']} шт. — {row['total'] / 100:.0f}₽\n"
    if not type_lines:
        type_lines = "  • Нет платежей за 30 дней\n"

    sales_source = await conn.fetch(
        """
        SELECT COALESCE(payment_source, 'bot') AS src,
               COUNT(*) AS cnt,
               COALESCE(SUM(amount), 0) AS total
        FROM payments
        WHERE status = 'completed' AND currency = 'RUB'
          AND timestamp >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY payment_source
        ORDER BY total DESC
        """
    )
    source_lines = ""
    source_labels = {
        "bot": "Бот",
        "miniapp": "Mini App",
        "yookassa_autopay": "Автоплатёж",
        "balance": "С баланса",
    }
    for row in sales_source:
        label = source_labels.get(row["src"], row["src"])
        source_lines += f"  • {label}: {row['cnt']} — {row['total'] / 100:.0f}₽\n"
    if not source_lines:
        source_lines = "  • Нет данных\n"

    expiring_no_card = await conn.fetchval(
        f"""
        SELECT COUNT(*) FROM users
        WHERE subscription_tier IN ({paid_tiers_sql})
          AND pay_subscribed = TRUE
          AND subscription_end IS NOT NULL
          AND DATE(subscription_end) BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days'
          AND DATE(subscription_end) < $1
          AND yookassa_recurring_payment_method_id IS NULL
        """,
        SENTINEL_SUBSCRIPTION_END_THRESHOLD,
    )

    trial_activated = await conn.fetchval(
        "SELECT COUNT(*) FROM users WHERE trial_used = TRUE"
    )
    trial_converted = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT p.user_id)
        FROM payments p
        JOIN users u ON u.user_id = p.user_id
        WHERE p.status = 'completed' AND u.trial_used = TRUE
        """
    )
    trial_rate = (
        (trial_converted or 0) / trial_activated * 100.0 if trial_activated else 0.0
    )

    total_keys = await conn.fetchval("SELECT COUNT(*) FROM vpn_keys")
    active_keys = await conn.fetchval(
        "SELECT COUNT(*) FROM vpn_keys WHERE is_active = TRUE"
    )
    total_referrals = await conn.fetchval(
        "SELECT COALESCE(SUM(referral_count), 0) FROM users"
    )
    total_servers = await conn.fetchval("SELECT COUNT(*) FROM servers")
    active_servers = await conn.fetchval(
        "SELECT COUNT(*) FROM servers WHERE is_active = TRUE"
    )

    bot_dau = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM bot_activity_logs
        WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
        """
    ) or 0
    bot_wau = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM bot_activity_logs
        WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        """
    ) or 0

    funnel_7d = await conn.fetchrow(
        """
        SELECT
            COUNT(*) AS reg,
            COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM bot_activity_logs b
                WHERE b.user_id = u.user_id AND b.action = 'get_vpn_link'
            )) AS vpn_click,
            COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM bot_activity_logs b
                WHERE b.user_id = u.user_id AND b.action = 'open_help'
            )) AS help_click,
            COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM subscription_usage_logs s WHERE s.user_id = u.user_id
            )) AS sub_req
        FROM users u
        WHERE u.blacklisted = FALSE
          AND u.registration_date >= CURRENT_DATE - INTERVAL '7 days'
        """
    ) or {}

    top_bot_actions = await conn.fetch(
        """
        SELECT
            SPLIT_PART(action, ':', 1) AS act,
            COUNT(*) AS cnt
        FROM bot_activity_logs
        WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        GROUP BY 1
        ORDER BY cnt DESC
        LIMIT 8
        """
    )
    bot_actions_text = ""
    for row in top_bot_actions:
        bot_actions_text += f"  • <code>{row['act'][:28]}</code>: {row['cnt']}\n"
    if not bot_actions_text:
        bot_actions_text = "  • Данных пока нет\n"

    reg_7d = int(funnel_7d.get("reg") or 0)
    vpn_click_7d = int(funnel_7d.get("vpn_click") or 0)
    help_click_7d = int(funnel_7d.get("help_click") or 0)
    sub_req_7d = int(funnel_7d.get("sub_req") or 0)
    funnel_pct = lambda n: f"{100 * n / reg_7d:.0f}%" if reg_7d else "—"

    top_platforms = await conn.fetch(
        """
        SELECT user_agent, COUNT(*) AS count
        FROM subscription_usage_logs
        WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        GROUP BY user_agent
        ORDER BY count DESC
        LIMIT 5
        """
    )
    platforms_text = ""
    for row in top_platforms:
        ua = (row["user_agent"] or "Unknown").split("/")[0].split(" ")[0][:15]
        platforms_text += f"  • {ua}: {row['count']} запр.\n"
    if not platforms_text:
        platforms_text = "  • Данных пока нет\n"

    bot_dau = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM bot_activity_logs
        WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
        """
    )
    bot_wau = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT user_id) FROM bot_activity_logs
        WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'
        """
    )
    top_bot_actions = await conn.fetch(
        """
        SELECT SPLIT_PART(action, ':', 1) AS act, COUNT(*) AS cnt
        FROM bot_activity_logs
        WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'
          AND event_kind IN ('callback', 'command')
        GROUP BY act
        ORDER BY cnt DESC
        LIMIT 8
        """
    )
    actions_text = ""
    for row in top_bot_actions:
        act = row["act"] or "?"
        label = ACTION_LABELS.get(act) or act[:20]
        actions_text += f"  • {label}: {row['cnt']}\n"
    if not actions_text:
        actions_text = "  • Данных пока нет\n"

    funnel = await conn.fetchrow(
        """
        WITH recent AS (
            SELECT user_id FROM users
            WHERE registration_date >= CURRENT_TIMESTAMP - INTERVAL '7 days'
              AND blacklisted = FALSE
        )
        SELECT
            (SELECT COUNT(*) FROM recent) AS reg,
            (SELECT COUNT(DISTINCT b.user_id)
             FROM bot_activity_logs b JOIN recent r ON r.user_id = b.user_id
             WHERE b.event_kind = 'callback') AS clicked_btn,
            (SELECT COUNT(DISTINCT b.user_id)
             FROM bot_activity_logs b JOIN recent r ON r.user_id = b.user_id
             WHERE b.action = 'get_vpn_link') AS vpn_btn,
            (SELECT COUNT(DISTINCT s.user_id)
             FROM subscription_usage_logs s JOIN recent r ON r.user_id = s.user_id) AS sub_req,
            (SELECT COUNT(DISTINCT u.user_id)
             FROM users u JOIN recent r ON r.user_id = u.user_id
             WHERE EXISTS (
                 SELECT 1 FROM bot_activity_logs b
                 WHERE b.user_id = u.user_id AND b.event_kind = 'command' AND b.action = '/start'
             )
             AND NOT EXISTS (
                 SELECT 1 FROM bot_activity_logs b
                 WHERE b.user_id = u.user_id AND b.event_kind = 'callback'
             )) AS only_start
        """
    )
    reg7 = funnel["reg"] or 0
    def _pct(n: int) -> str:
        if not reg7:
            return "0%"
        return f"{100 * n / reg7:.0f}%"

    msk = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")

    return (
        "📊 <b>Статистика SvoyVPN</b>\n\n"
        "<b>👥 Пользователи</b>\n"
        f"• Всего: <b>{total_users}</b>\n"
        f"• Free (активных): <b>{free_users or 0}</b>\n"
        f"• Plus (активных): <b>{plus_users or 0}</b>\n"
        + (f"• Legacy тарифы: <i>{legacy_paid}</i>\n" if legacy_paid else "")
        + f"• С привязанной картой: <b>{with_card or 0}</b>\n"
        f"• В отсрочке автоплатежа: <b>{in_grace or 0}</b>\n"
        f"• Новых сегодня / за 7д: <b>{new_today or 0}</b> / <b>{new_week or 0}</b>\n\n"
        "<b>📈 Активность</b>\n"
        f"• В боте 7д / 30д: <b>{active_7d or 0}</b> / <b>{active_30d or 0}</b>\n"
        f"• Неактивны 30+ дней: <i>{inactive_30d or 0}</i>\n"
        f"• VPN DAU / WAU / MAU: <b>{sub_dau or 0}</b> / <b>{sub_wau or 0}</b> / <b>{sub_mau or 0}</b>\n"
        f"• Запросов /sub сегодня: <b>{sub_requests_today or 0}</b>\n"
        f"• В боте (клики/команды) DAU / WAU: <b>{bot_dau or 0}</b> / <b>{bot_wau or 0}</b>\n\n"
        "<b>🧭 Воронка (7 дней)</b>\n"
        f"• Регистраций: <b>{reg7}</b>\n"
        f"• Нажали кнопку: <b>{funnel['clicked_btn'] or 0}</b> ({_pct(funnel['clicked_btn'] or 0)})\n"
        f"• «Подключить VPN»: <b>{funnel['vpn_btn'] or 0}</b> ({_pct(funnel['vpn_btn'] or 0)})\n"
        f"• Запросили /sub: <b>{funnel['sub_req'] or 0}</b> ({_pct(funnel['sub_req'] or 0)})\n"
        f"• Только /start, без кнопок: <i>{funnel['only_start'] or 0}</i>\n\n"
        "<b>🔝 Действия в боте (7д)</b>\n"
        f"{actions_text}\n"
        "<b>📱 Клиенты (7д)</b>\n"
        f"{platforms_text}\n"
        "<b>💰 Финансы (30 дней)</b>\n"
        f"• Доход: <b>{(revenue_30d_rub or 0) / 100:.0f}₽</b> "
        f"(сегодня {(revenue_today_rub or 0) / 100:.0f}₽, {payments_today or 0} пл.)\n"
        f"• Всего за всё время: {(total_revenue_rub or 0) / 100:.0f}₽\n"
        f"• Платили когда-либо: <b>{paying_users or 0}</b>\n"
        f"• ARPU / ARPPU 30д: {arpu_30d:.1f}₽ / {arppu_30d:.1f}₽\n\n"
        "<b>По типу платежа:</b>\n"
        f"{type_lines}\n"
        "<b>По источнику:</b>\n"
        f"{source_lines}\n"
        "<b>📉 Plus без карты</b> (истекают ≤7д): "
        f"<b>{expiring_no_card or 0}</b>\n\n"
        "<b>🧪 Пробный Plus</b>\n"
        f"• Активировали: {trial_activated or 0}\n"
        f"• Оплатили после: {trial_converted or 0} ({trial_rate:.0f}%)\n\n"
        "<b>🔑 VPN</b>\n"
        f"• Ключей активных / всего: {active_keys or 0} / {total_keys or 0}\n"
        f"• Серверов активных: {active_servers or 0} / {total_servers or 0}\n\n"
        "<b>🎁 Рефералы</b>\n"
        f"• Приглашений всего: {total_referrals or 0}\n\n"
        f"🕒 {msk}"
    )
