"""
Обработчики системы тарифов (Free / Plus).
Покупка Plus, докупка bypass ГБ.
"""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime

from aiogram import Bot, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    LabeledPrice,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import AppConfig
from ..database import get_connection
from ..plans import (
    ALL_PAID_TIER_IDS,
    FREE_TIER_ID,
    PAID_TIER_IDS,
    PAYMENT_METHODS,
    TIER_ORDER,
    TIER_PLANS_BASE,
    TIERS,
    format_price_both,
    format_price_both_button,
    format_price_stars_button,
    format_price_rub,
    format_price_stars,
    format_subscription_end_for_display,
    format_tier_monthly_price_button,
    format_tier_monthly_price_html,
    get_bypass_packs,
    get_tier_bypass_gb,
    get_tier_max_devices,
    get_tier_plans,
    get_tier_plans_for_tier,
)
from ..traffic import user_bypass_traffic_snapshot
from ..yookassa_client import YooKassaClient
from ..custom_emojis import E, e, lbl, btn, emoji_button, raw

logger = logging.getLogger(__name__)


async def _load_tier_screen_context(user_id: int) -> dict:
    """Контекст экрана подписки: тариф, статус, цены Plus."""
    plans = await get_tier_plans()
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT subscription_tier, pay_subscribed, subscription_end,
                   yookassa_recurring_payment_method_id
            FROM users WHERE user_id = $1
            """,
            user_id,
        )

    from ..plans import is_sentinel_subscription_end

    current_tier = (row["subscription_tier"] if row else None) or "none"
    sub_end = row["subscription_end"] if row else None
    paid_sentinel = (
        current_tier in ALL_PAID_TIER_IDS
        and bool(sub_end)
        and is_sentinel_subscription_end(sub_end)
    )
    is_active = bool(
        row
        and row["pay_subscribed"]
        and sub_end
        and not paid_sentinel
        and sub_end.date() >= datetime.now().date()
    )
    has_card = bool(row and row.get("yookassa_recurring_payment_method_id"))
    is_plus = current_tier in ALL_PAID_TIER_IDS

    plan_1m = plans.get("plus_1m") or TIER_PLANS_BASE.get("plus_1m", {})
    plan_12m = plans.get("plus_12m") or TIER_PLANS_BASE.get("plus_12m", {})

    return {
        "current_tier": current_tier,
        "sub_end": sub_end,
        "is_active": is_active,
        "has_card": has_card,
        "is_renewable": is_active and has_card,
        "is_plus": is_plus,
        "plan_1m": plan_1m,
        "plan_12m": plan_12m,
        "price_1m": plan_1m.get("price_rub", 14900),
        "price_12m": plan_12m.get("price_rub", 99900),
    }


def _tier_header_sections(ctx: dict) -> list[str]:
    """Верх экрана: заголовок и строка о текущем тарифе (одинаково на всех шагах)."""
    sections: list[str] = [f"{E.subscription} <b>Подписка VPN</b>"]
    if ctx["is_active"] and ctx["is_plus"]:
        if ctx["has_card"]:
            sections.append("Тариф <b>Plus</b> подключен")
        else:
            end_str = format_subscription_end_for_display(ctx["sub_end"])
            if end_str:
                sections.append(f"Тариф <b>Plus</b> подключен до {end_str}")
            else:
                sections.append("Тариф <b>Plus</b> подключен")
    elif ctx["is_active"] and ctx["current_tier"] == FREE_TIER_ID:
        sections.append("Тариф: <b>Free</b> (бесплатный)")
    return sections


async def build_tiers_message(user_id: int, *, view: str = "main"):
    """
    Экран подписки.
    view=main — Free и Plus, кнопка «Plus».
    view=plus_plans — варианты Plus с ценами (месяц / год).
    view=referral_trial — Plus за 1₽ (реферал/UTM).
    """
    ctx = await _load_tier_screen_context(user_id)
    sections = _tier_header_sections(ctx)

    from ..trial_usage import (
        has_completed_trial_payment,
        user_show_referral_trial_offer,
    )

    show_referral_trial = False
    trial_used = False
    async with get_connection() as conn:
        show_referral_trial = await user_show_referral_trial_offer(conn, user_id)
        trial_used = await has_completed_trial_payment(conn, user_id)

    plus_t = TIERS["plus"]
    plus_features = [f"  • {f}" for f in plus_t["features"]]

    if view == "referral_trial":
        from ..trial_usage import get_trial_days

        async with get_connection() as conn:
            trial_days = await get_trial_days(conn)
        sections.append(
            f"{E.gift} <b>Специальное предложение</b>\n"
            f"Plus на <b>{trial_days} дней</b> за <b>1₽</b> — только для тех, "
            f"кто пришёл по реферальной или партнёрской ссылке.\n\n"
            + "\n".join([f"<b>Plus</b>"] + plus_features)
        )
    elif view == "plus_plans":
        per_month_12m = ctx["price_12m"] // 1200
        plus_1m_lines = (
            [f"<b>Plus</b> · <b>{format_price_rub(ctx['price_1m'])}/мес</b>"] + plus_features
        )
        plus_12m_lines = (
            [
                f"<b>Plus</b> · <b>{format_price_rub(ctx['price_12m'])}/год</b> "
                f"(<b>{per_month_12m}₽/мес</b>)"
            ]
            + plus_features
        )
        sections.append("\n".join(plus_1m_lines))
        sections.append("\n".join(plus_12m_lines))
    else:
        free_t = TIERS["free"]
        free_marker = " ← ваш" if (ctx["is_active"] and not ctx["is_plus"]) else ""
        free_lines = [f"<b>Free</b>{free_marker}"] + [
            f"  • {f}" for f in free_t["features"]
        ]
        plus_marker = " ← ваш" if (ctx["is_active"] and ctx["is_plus"]) else ""
        plus_lines = [f"<b>Plus</b>{plus_marker}"] + plus_features
        sections.append("\n".join(free_lines))
        sections.append("\n".join(plus_lines))
        if show_referral_trial:
            sections.append(f"{E.gift} <b>Plus за 1₽</b> — специальное предложение для вас")

    text = "\n\n".join(sections)
    builder = InlineKeyboardBuilder()

    if view == "referral_trial":
        builder.row(
            btn("Plus за 1₽ — попробовать", "gift",
                callback_data="activate_trial",
            ),
        )
        builder.row(
            btn("Назад", "back", callback_data="open_tiers"),
        )
    elif view == "plus_plans":
        builder.row(
            btn(f"Plus · {format_price_rub(ctx['price_1m'])}/мес", "plus",
                callback_data="tier_select:plus:plus_1m",
            ),
            btn(f"Plus · {format_price_rub(int(ctx['price_12m'] / 12))}/мес", "star_plus",
                callback_data="tier_select:plus:plus_12m",
            ),
        )
        if trial_used:
            builder.row(
                btn("Пригласи друга — получи бонус", "gift",
                    callback_data="open_invite",
                ),
            )
        builder.row(
            btn("Назад", "back", callback_data="open_tiers"),
        )
    elif ctx["is_active"] and ctx["is_plus"]:
        if ctx["has_card"]:
            builder.row(
                btn("Free (отменить)", "free", callback_data="cancel_sub_start"
                ),
            )
            builder.row(
                btn("Plus — подключен", "success", callback_data="tier_info:plus"
                ),
                btn("Лимиты", "limits", callback_data="open_bypass_packs"
                ),
            )
        else:
            builder.row(
                btn("Plus — продлить", "plus",
                    callback_data="tier_select:plus",
                ),
            )
            builder.row(
                btn("Лимиты", "limits", callback_data="open_bypass_packs"
                ),
            )
        builder.row(
            btn("Назад", "back", callback_data="go_back_subscription"),
        )
    else:
        builder.row(
            btn("Free — ваш", "success", callback_data=f"tier_info:{FREE_TIER_ID}")
            if (ctx["is_active"] and not ctx["is_plus"])
            else btn("Free — бесплатно", "free", callback_data=f"tier_info:{FREE_TIER_ID}")
        )
        if show_referral_trial:
            builder.row(
                btn("Plus за 1₽ — попробовать", "gift",
                    callback_data="activate_trial",
                ),
            )
        else:
            builder.row(
                btn("Plus", "plus", callback_data="tier_select:plus"),
            )
            if trial_used:
                builder.row(
                    btn("Пригласи друга — получи бонус", "gift",
                        callback_data="open_invite",
                    ),
                )
        builder.row(
            btn("Назад", "back", callback_data="go_back_subscription"),
        )

    return text, builder.as_markup()


async def setup_tier_handlers(dp, bot: Bot, config: AppConfig):
    """Register all tier-related handlers."""

    # ------------------------------------------------------------------
    # Tier selection menu
    # ------------------------------------------------------------------
    @dp.callback_query(F.data == "open_tiers")
    async def handle_open_tiers(callback: CallbackQuery):
        """Main tier selection screen."""
        user_id = callback.from_user.id
        text, markup = await build_tiers_message(user_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await callback.answer()

    # ------------------------------------------------------------------
    # Tier details + duration selection
    # ------------------------------------------------------------------
    @dp.callback_query(F.data.startswith("tier_select:"))
    async def handle_tier_select(callback: CallbackQuery):
        """tier_select:plus — выбор срока; tier_select:plus:plus_1m — оплата."""
        parts = callback.data.split(":")
        if len(parts) == 2 and parts[1] == "plus":
            user_id = callback.from_user.id
            async with get_connection() as conn:
                from ..trial_usage import user_show_referral_trial_offer
                if await user_show_referral_trial_offer(conn, user_id):
                    text, markup = await build_tiers_message(
                        user_id, view="referral_trial"
                    )
                    await callback.message.edit_text(
                        text, parse_mode="HTML", reply_markup=markup
                    )
                    await callback.answer()
                    return
            text, markup = await build_tiers_message(user_id, view="plus_plans")
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            await callback.answer()
            return
        if len(parts) >= 3:
            plan_id = parts[2]
            await _do_tier_pay(callback, plan_id)
            return
        await callback.answer(f"{E.error} Нет доступных планов", show_alert=True)

    # ------------------------------------------------------------------
    # Buy tier (legacy callback kept for backward compat, redirects to pay)
    # ------------------------------------------------------------------
    @dp.callback_query(F.data.startswith("tier_buy:"))
    async def handle_tier_buy(callback: CallbackQuery):
        """Redirect old tier_buy callbacks to tier_pay."""
        plan_id = callback.data.split(":")[1]
        await _do_tier_pay(callback, plan_id)

    # ------------------------------------------------------------------
    # Process tier payment
    # ------------------------------------------------------------------
    async def _do_tier_pay(callback: CallbackQuery, plan_id: str):
        """Показать описание тарифа + сразу создать платёж и кнопку оплаты."""
        user_id = callback.from_user.id

        plans = await get_tier_plans()
        if plan_id not in plans:
            await callback.answer(f"{E.error} План не найден", show_alert=True)
            return

        plan = plans[plan_id]
        tier_id = plan["tier"]
        t = TIERS.get(tier_id, {})

        if not config.yookassa.enabled:
            await callback.answer(f"{E.error} ЮKassa не настроена", show_alert=True)
            return
        price = plan["price_rub"]

        if price < 100:
            price = 100
        try:
            yk = YooKassaClient(config.yookassa)
            bot_info = await bot.get_me()
            amount_rub = price / 100.0
            payment_data = yk.create_payment(
                amount=amount_rub,
                description=f"VPN {plan['title']}",
                return_url=f"https://t.me/{bot_info.username}?start=payment_success",
                metadata={
                    "user_id": str(user_id),
                    "plan_id": plan_id,
                    "method_id": "yookassa",
                    "product_type": "tier",
                },
                save_payment_method=True,
                merchant_customer_id=str(user_id),
            )
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    user_id, price, "RUB", plan_id, "tier", "pending",
                    payment_data["id"],
                )

            duration_months = plan.get("duration", 1)
            price_display = (
                f"{format_price_rub(price)}/год" if duration_months >= 12
                else f"{format_price_rub(price)}/мес"
            )
            text = f"{E.plus} <b>{t.get('name', tier_id)}</b> · {price_display}\n\n"
            for feat in t.get("features", []):
                text += f"• {feat}\n"

            b = InlineKeyboardBuilder()
            b.row(btn("Перейти к оплате", "card", url=payment_data["confirmation_url"]))
            b.row(btn("Назад", "back", callback_data="tier_select:plus"))
            await callback.message.edit_text(
                text, parse_mode="HTML", reply_markup=b.as_markup()
            )
            await callback.answer()
        except Exception as e:
            logger.error("tier yookassa error: %s", e, exc_info=True)
            await callback.answer(f"{E.error} Ошибка создания платежа", show_alert=True)

    @dp.callback_query(F.data.startswith("tier_pay:"))
    async def handle_tier_pay(callback: CallbackQuery):
        """Router for tier_pay: callbacks."""
        parts = callback.data.split(":")
        if len(parts) < 2:
            await callback.answer(f"{E.error} Ошибка данных", show_alert=True)
            return
        plan_id = parts[1]
        await _do_tier_pay(callback, plan_id)

    # ------------------------------------------------------------------
    # Promo discount handlers (from engagement notifications)
    # ------------------------------------------------------------------
    @dp.callback_query(F.data == "promo_plus_30")
    async def handle_promo_plus_30(callback: CallbackQuery):
        """30% discount on Plus from engagement notification."""
        user_id = callback.from_user.id
        plans = await get_tier_plans()
        plan = plans.get("plus_1m")
        if not plan:
            await callback.answer(f"{E.error} План не найден", show_alert=True)
            return
        price = int(plan["price_rub"] * 0.7)
        if price < 100:
            price = 100
        try:
            yk = YooKassaClient(config.yookassa)
            bot_info = await bot.get_me()
            amount_rub = price / 100.0
            payment_data = yk.create_payment(
                amount=amount_rub,
                description="VPN Plus — скидка 30%",
                return_url=f"https://t.me/{bot_info.username}?start=payment_success",
                metadata={
                    "user_id": str(user_id),
                    "plan_id": "plus_1m",
                    "method_id": "yookassa",
                    "product_type": "tier",
                },
                save_payment_method=True,
                merchant_customer_id=str(user_id),
            )
            async with get_connection() as conn:
                await conn.execute(
                    """INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    user_id, price, "RUB", "plus_1m", "tier", "pending",
                    payment_data["id"],
                )
            b = InlineKeyboardBuilder()
            b.row(btn("Перейти к оплате", "card", url=payment_data["confirmation_url"]))
            b.row(btn("К тарифам", "back", callback_data="open_tiers"))
            full_price = plan["price_rub"] / 100.0
            await callback.message.edit_text(
                f"{E.hot} <b>Plus со скидкой 30%</b>\n\n"
                f"<s>{full_price:.0f}₽</s> → <b>{amount_rub:.0f}₽/мес</b>\n\n"
                f"• 30 ГБ bypass/мес\n• YouTube / TikTok / AI работают\n• Безлимит устройств",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            await callback.answer()
        except Exception as e:
            logger.error("promo_plus_30 error: %s", e, exc_info=True)
            await callback.answer(f"{E.error} Ошибка", show_alert=True)

    # ------------------------------------------------------------------
    # Tier info (current tier details)
    # ------------------------------------------------------------------
    @dp.callback_query(F.data.startswith("tier_info:"))
    async def handle_tier_info(callback: CallbackQuery):
        """Show current tier details with bypass usage."""
        user_id = callback.from_user.id
        requested_tier = callback.data.split(":", 1)[1] if ":" in (callback.data or "") else ""

        async with get_connection() as conn:
            snap = await user_bypass_traffic_snapshot(conn, user_id)
            row = await conn.fetchrow(
                """
                SELECT subscription_end, subscription_tier,
                       yookassa_recurring_payment_method_id, pay_subscribed
                FROM users WHERE user_id = $1
                """,
                user_id,
            )

        tier_id = requested_tier if requested_tier in TIERS else snap["tier"]
        if tier_id in ALL_PAID_TIER_IDS and tier_id != FREE_TIER_ID:
            tier_id = "plus"
        tier_info = TIERS.get(tier_id, TIERS["plus"])
        has_card = bool(row and row.get("yookassa_recurring_payment_method_id"))
        actual_tier = snap.get("tier") or (row["subscription_tier"] if row else FREE_TIER_ID)

        if tier_id == FREE_TIER_ID and actual_tier in ALL_PAID_TIER_IDS:
            free_t = TIERS[FREE_TIER_ID]
            preview = (
                f"{E.free} <b>{free_t['name']}</b>\n\n"
                "Переход на бесплатный тариф — через <b>отмену</b> платной подписки. "
                "До конца оплаченного периода останется текущий тариф.\n\n"
                + "\n".join(f"• {f}" for f in free_t["features"])
            )
            b = InlineKeyboardBuilder()
            if has_card:
                b.row(
                    btn("Отменить подписку", "error",
                        callback_data="cancel_sub_start",
                    )
                )
            b.row(btn("К тарифам", "back", callback_data="open_tiers"))
            await callback.message.edit_text(
                preview, parse_mode="HTML", reply_markup=b.as_markup()
            )
            await callback.answer()
            return

        end_str = "—"
        if row and row["subscription_end"]:
            end_str = (
                format_subscription_end_for_display(row["subscription_end"]) or "—"
            )

        used_gb = snap["bypassUsedGb"]
        limit_gb = snap["bypassLimitGb"]
        remaining_gb = snap["bypassRemainingGb"]
        percent = snap["bypassPercentUsed"]

        text = (
            f"{E.chart} <b>Ваш тариф: {tier_info.get('name', tier_id)}</b>\n\n"
            f"{E.calendar} Подписка до: <b>{end_str}</b>\n\n"
            f"{E.bypass} <b>Bypass трафик:</b>\n"
            f"  Использовано: {used_gb:.1f} / {limit_gb:.0f} ГБ ({percent:.0f}%)"
        )
        from ..help_urls import bypass_help_link_html
        if snap["bypassBonusGb"] > 0:
            text += f" (+{snap['bypassBonusGb']} ГБ пакет)"
        text += bypass_help_link_html() + (
            f"\n  Осталось: <b>{remaining_gb:.1f} ГБ</b>\n"
        )
        max_dev = tier_info.get("max_devices", 1)
        devices_display = "безлимит" if max_dev >= 999 else f"до {max_dev}"
        text += f"\n{E.globe} Обычный VPN: <b>безлимит</b>\n"
        text += f"{E.devices} Устройств: {devices_display}\n"

        if snap["bypassExceeded"]:
            text += f"\n{E.warning} <b>Bypass лимит исчерпан!</b> Докупите ГБ или перейдите на Plus.\n"

        is_paid_active = tier_id in ALL_PAID_TIER_IDS and has_card

        builder = InlineKeyboardBuilder()
        if tier_id != FREE_TIER_ID:
            builder.row(
                btn("Лимиты", "limits",
                    callback_data="open_bypass_packs",
                )
            )
        elif tier_id == FREE_TIER_ID:
            builder.row(
                btn("Перейти на Plus", "subscription",
                    callback_data="open_tiers",
                )
            )
        if is_paid_active:
            builder.row(
                btn("Отменить подписку", "error",
                    callback_data="cancel_sub_start",
                )
            )
        builder.row(
            btn("К тарифам", "back", callback_data="open_tiers")
        )

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
            disable_web_page_preview=True,
        )
        await callback.answer()

    # ------------------------------------------------------------------
    # Cancel subscription retention flow
    # ------------------------------------------------------------------
    @dp.callback_query(F.data == "cancel_sub_start")
    async def handle_cancel_sub_start(callback: CallbackQuery):
        """Step 1: Offer 50% discount on current or higher tier."""
        user_id = callback.from_user.id

        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT subscription_tier, cancel_retention_used FROM users WHERE user_id = $1",
                user_id,
            )

        if not row or not row["subscription_tier"] or row["subscription_tier"] == "none":
            await callback.answer("У вас нет активной подписки", show_alert=True)
            return

        current_tier = row["subscription_tier"]
        already_used = row.get("cancel_retention_used") or False

        if already_used:
            # Already used retention offer — only offer 10 GB
            text = (
                f"{E.sad} <b>Жаль, что вы хотите уйти</b>\n\n"
                "Мы можем добавить вам <b>+10 ГБ bypass</b> прямо сейчас.\n"
            )
            b = InlineKeyboardBuilder()
            b.row(btn("Получить +10 ГБ", "gift", callback_data="cancel_accept_10gb"))
            b.row(btn("Всё равно отменить", "error", callback_data="cancel_sub_final"))
            b.row(btn("Назад", "back", callback_data=f"tier_info:{current_tier}"))
        else:
            # First time — offer 50% discount on Plus
            text = (
                f"{E.sad} <b>Жаль, что вы хотите уйти</b>\n\n"
                "Специально для вас — <b>скидка 50%</b> на следующий месяц!\n"
            )
            b = InlineKeyboardBuilder()
            plans = await get_tier_plans()
            plan_id = "plus_1m"
            plan = plans.get(plan_id)
            if plan:
                half_price = plan["price_rub"] // 2
                b.row(btn(f"Plus за {format_price_rub(half_price)}", "hot",
                    callback_data=f"cancel_offer_50:{plan_id}",
                ))

            b.row(btn("Всё равно отменить", "error", callback_data="cancel_sub_step2"))
            b.row(btn("Назад", "back", callback_data=f"tier_info:{current_tier}"))

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("cancel_offer_50:"))
    async def handle_cancel_offer_50(callback: CallbackQuery):
        """User accepted 50% discount — create payment at half price."""
        plan_id = callback.data.split(":")[1]
        user_id = callback.from_user.id

        plans = await get_tier_plans()
        if plan_id not in plans:
            await callback.answer(f"{E.error} План не найден", show_alert=True)
            return

        plan = plans[plan_id]
        half_price = plan["price_rub"] // 2
        if half_price < 100:
            half_price = 100

        try:
            yk = YooKassaClient(config.yookassa)
            bot_info = await bot.get_me()
            payment_data = yk.create_payment(
                amount=half_price / 100.0,
                description=f"VPN {plan['title']} (скидка 50%)",
                return_url=f"https://t.me/{bot_info.username}?start=payment_success",
                metadata={
                    "user_id": str(user_id),
                    "plan_id": plan_id,
                    "method_id": "yookassa",
                    "product_type": "tier",
                },
                save_payment_method=True,
                merchant_customer_id=str(user_id),
            )
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    user_id, half_price, "RUB", plan_id, "tier", "pending",
                    payment_data["id"],
                )
                await conn.execute(
                    "UPDATE users SET cancel_retention_used = TRUE WHERE user_id = $1",
                    user_id,
                )

            b = InlineKeyboardBuilder()
            b.row(btn("Перейти к оплате", "card", url=payment_data["confirmation_url"]))
            b.row(btn("Назад", "back", callback_data="open_tiers"))
            await callback.message.edit_text(
                f"{E.hot} <b>{plan['title']}</b> со скидкой 50%\n\n"
                f"Сумма: <b>{format_price_rub(half_price)}</b>",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            await callback.answer()
        except Exception as e:
            logger.error("cancel_offer_50 error: %s", e, exc_info=True)
            await callback.answer(f"{E.error} Ошибка", show_alert=True)

    @dp.callback_query(F.data == "cancel_sub_step2")
    async def handle_cancel_sub_step2(callback: CallbackQuery):
        """Step 2: Offer 100 GB bonus bypass."""
        user_id = callback.from_user.id
        async with get_connection() as conn:
            tier = await conn.fetchval(
                "SELECT subscription_tier FROM users WHERE user_id = $1", user_id
            )

        text = (
            f"{E.gift} <b>Подождите!</b>\n\n"
            "Мы добавим вам <b>+100 ГБ bypass</b> бесплатно прямо сейчас.\n"
            "Они суммируются с вашим текущим лимитом."
        )
        b = InlineKeyboardBuilder()
        b.row(btn("Отлично, забираю!", "gift", callback_data="cancel_accept_100gb"))
        b.row(btn("Нет, отменить", "error", callback_data="cancel_sub_step3"))
        b.row(btn("Назад", "back", callback_data="cancel_sub_start"))

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
        await callback.answer()

    @dp.callback_query(F.data == "cancel_accept_100gb")
    async def handle_cancel_accept_100gb(callback: CallbackQuery):
        """User accepted 100 GB bonus."""
        user_id = callback.from_user.id
        async with get_connection() as conn:
            from ..tier_payments import apply_bypass_pack
            await apply_bypass_pack(conn, user_id, 100)
            await conn.execute(
                "UPDATE users SET cancel_retention_used = TRUE WHERE user_id = $1",
                user_id,
            )
        await callback.message.edit_text(
            f"{E.success} <b>Готово!</b>\n\n"
            "+100 ГБ bypass добавлены на ваш аккаунт.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().row(
                btn("На главную", "home", callback_data="go_back")
            ).as_markup(),
        )
        await callback.answer()

    @dp.callback_query(F.data == "cancel_accept_10gb")
    async def handle_cancel_accept_10gb(callback: CallbackQuery):
        """User accepted 10 GB bonus (repeat canceller)."""
        user_id = callback.from_user.id
        async with get_connection() as conn:
            from ..tier_payments import apply_bypass_pack
            await apply_bypass_pack(conn, user_id, 10)
        await callback.message.edit_text(
            f"{E.success} <b>Готово!</b>\n\n"
            "+10 ГБ bypass добавлены на ваш аккаунт.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().row(
                btn("На главную", "home", callback_data="go_back")
            ).as_markup(),
        )
        await callback.answer()

    @dp.callback_query(F.data == "cancel_sub_step3")
    async def handle_cancel_sub_step3(callback: CallbackQuery):
        """Step 3: Offer Plus for 1₽/month."""
        text = (
            f"{E.money} <b>Последнее предложение!</b>\n\n"
            "Тариф <b>Plus</b> всего за <b>1₽</b> на следующий месяц:\n\n"
            "• 30 ГБ bypass/мес\n"
            "• YouTube / TikTok / AI работают\n"
            "• Безлимит устройств"
        )
        b = InlineKeyboardBuilder()
        b.row(btn("Plus за 1₽", "plus",
            callback_data="cancel_offer_1rub:plus_1m",
        ))
        b.row(btn("Нет, отменить подписку", "error", callback_data="cancel_sub_final"))
        b.row(btn("Назад", "back", callback_data="cancel_sub_step2"))

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("cancel_offer_1rub:"))
    async def handle_cancel_offer_1rub(callback: CallbackQuery):
        """User accepted 1₽ offer — create payment for 1 RUB."""
        plan_id = callback.data.split(":")[1]
        user_id = callback.from_user.id

        plans = await get_tier_plans()
        if plan_id not in plans:
            await callback.answer(f"{E.error} План не найден", show_alert=True)
            return

        plan = plans[plan_id]
        price_kopecks = 100  # 1₽

        try:
            yk = YooKassaClient(config.yookassa)
            bot_info = await bot.get_me()
            payment_data = yk.create_payment(
                amount=1.00,
                description=f"VPN {plan['title']} (спецпредложение)",
                return_url=f"https://t.me/{bot_info.username}?start=payment_success",
                metadata={
                    "user_id": str(user_id),
                    "plan_id": plan_id,
                    "method_id": "yookassa",
                    "product_type": "tier",
                },
                save_payment_method=True,
                merchant_customer_id=str(user_id),
            )
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    user_id, price_kopecks, "RUB", plan_id, "tier", "pending",
                    payment_data["id"],
                )
                await conn.execute(
                    "UPDATE users SET cancel_retention_used = TRUE WHERE user_id = $1",
                    user_id,
                )

            b = InlineKeyboardBuilder()
            b.row(btn("Перейти к оплате", "card", url=payment_data["confirmation_url"]))
            b.row(btn("Назад", "back", callback_data="open_tiers"))
            await callback.message.edit_text(
                f"{E.hot} <b>{plan['title']}</b> за 1₽\n\n"
                f"Сумма: <b>1₽</b>",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            await callback.answer()
        except Exception as e:
            logger.error("cancel_offer_1rub error: %s", e, exc_info=True)
            await callback.answer(f"{E.error} Ошибка", show_alert=True)

    @dp.callback_query(F.data == "cancel_sub_final")
    async def handle_cancel_sub_final(callback: CallbackQuery):
        """Final cancellation: remove saved card and deactivate autopay."""
        user_id = callback.from_user.id
        async with get_connection() as conn:
            await conn.execute(
                """
                UPDATE users
                SET yookassa_recurring_payment_method_id = NULL,
                    cancel_retention_used = FALSE
                WHERE user_id = $1
                """,
                user_id,
            )

        await callback.message.edit_text(
            f"{E.success} <b>Подписка отменена</b>\n\n"
            "Автоматическое продление отключено. "
            "Текущая подписка будет действовать до конца оплаченного периода.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().row(
                btn("На главную", "home", callback_data="go_back")
            ).as_markup(),
        )
        await callback.answer()

    # ------------------------------------------------------------------
    # Bypass GB pack purchase
    # ------------------------------------------------------------------
    @dp.callback_query(F.data == "open_bypass_packs")
    async def handle_open_bypass_packs(callback: CallbackQuery):
        """Show bypass GB pack purchase menu."""
        user_id = callback.from_user.id

        async with get_connection() as conn:
            ok_sub = await conn.fetchval(
                """
                SELECT CASE
                    WHEN pay_subscribed = TRUE AND subscription_end IS NOT NULL
                         AND DATE(subscription_end) >= CURRENT_DATE
                    THEN TRUE ELSE FALSE END
                FROM users WHERE user_id = $1
                """,
                user_id,
            )
            snap = await user_bypass_traffic_snapshot(conn, user_id)

        packs = await get_bypass_packs()

        if not ok_sub:
            text = (
                f"{E.signal} <b>Докупка bypass трафика</b>\n\n"
                "Доступно только при <b>активной подписке</b>.\n"
                "Оформите подписку в разделе «Подписка»."
            )
            b = InlineKeyboardBuilder()
            b.row(btn("Выбрать тариф", "plus", callback_data="open_tiers"))
            b.row(btn("Назад", "back", callback_data="go_back_subscription"))
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
            await callback.answer()
            return

        remaining = snap["bypassRemainingGb"]
        limit = snap["bypassLimitGb"]
        used = snap["bypassUsedGb"]

        text = (
            f"{E.signal} <b>Докупить bypass трафик</b>\n\n"
            f"Текущий остаток: <b>{remaining:.1f} ГБ</b> из {limit:.0f} ГБ\n"
            f"Использовано: {used:.1f} ГБ\n\n"
        )
        if not packs:
            text += "Пакеты сейчас недоступны."
        else:
            text += "Дополнительные ГБ суммируются с лимитом:\n\n"
            for p in packs:
                text += f"• <b>+{p['gb_amount']} ГБ</b> — {format_price_rub(p['price_rub'])}\n"

        builder = InlineKeyboardBuilder()
        if packs:
            for p in packs:
                builder.row(
                    InlineKeyboardButton(
                        text=f"+{p['gb_amount']} ГБ — {format_price_both_button(p['price_rub'], p['price_stars'])}",
                        callback_data=f"bypass_pack_choose:{p['id']}",
                    )
                )

        builder.row(btn("Назад", "back", callback_data="open_tiers"))
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("bypass_pack_choose:"))
    async def handle_bypass_pack_choose(callback: CallbackQuery):
        """Choose payment method for bypass pack."""
        user_id = callback.from_user.id
        try:
            pack_id = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await callback.answer(f"{E.error} Ошибка", show_alert=True)
            return

        async with get_connection() as conn:
            pack = await conn.fetchrow(
                "SELECT id, title, gb_amount, price_rub, price_stars FROM bypass_pack_products WHERE id = $1 AND is_active = TRUE",
                pack_id,
            )
        if not pack:
            await callback.answer(f"{E.error} Пакет недоступен", show_alert=True)
            return

        text = (
            f"{E.signal} <b>Оплата: +{pack['gb_amount']} ГБ bypass</b>\n\n"
            f"{pack['title']}\n\n"
            f"Выберите способ оплаты:"
        )
        b = InlineKeyboardBuilder()
        if int(pack["price_stars"] or 0) >= 1:
            b.row(
                btn(f"Stars ({format_price_stars_button(pack['price_stars'])})", "star",
                    callback_data=f"bypass_pack_pay:{pack_id}:stars",
                )
            )
        if config.yookassa.enabled and int(pack["price_rub"] or 0) >= 100:
            b.row(
                btn(f"Карта ({format_price_rub(pack['price_rub'])})", "card",
                    callback_data=f"bypass_pack_pay:{pack_id}:yookassa",
                )
            )
        b.row(btn("Назад", "back", callback_data="open_bypass_packs"))
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("bypass_pack_pay:"))
    async def handle_bypass_pack_pay(callback: CallbackQuery):
        """Process bypass pack payment."""
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer(f"{E.error} Ошибка", show_alert=True)
            return

        pack_id = int(parts[1])
        method_id = parts[2]
        user_id = callback.from_user.id

        async with get_connection() as conn:
            pack = await conn.fetchrow(
                "SELECT id, title, gb_amount, price_rub, price_stars FROM bypass_pack_products WHERE id = $1 AND is_active = TRUE",
                pack_id,
            )
        if not pack:
            await callback.answer(f"{E.error} Пакет недоступен", show_alert=True)
            return

        if method_id == "stars":
            price = int(pack["price_stars"] or 0)
            if price < 1:
                await callback.answer(f"{E.error} Неверная цена", show_alert=True)
                return
            ts = int(time.time())
            payload = f"bypass_pack|{user_id}|{pack_id}|{ts}"
            try:
                await bot.send_invoice(
                    chat_id=callback.message.chat.id,
                    title=f"Bypass: +{pack['gb_amount']} ГБ",
                    description=f"Дополнительный bypass трафик",
                    provider_token="",
                    currency="XTR",
                    prices=[LabeledPrice(label=str(pack["title"])[:32], amount=price)],
                    payload=payload,
                    start_parameter=f"bp{pack_id}",
                )
                await callback.answer()
            except Exception as e:
                logger.error("bypass pack stars: %s", e, exc_info=True)
                await callback.answer(f"{E.error} Ошибка создания счёта", show_alert=True)

        elif method_id == "yookassa":
            price = int(pack["price_rub"] or 0)
            if price < 100:
                await callback.answer(f"{E.error} Минимум 1₽", show_alert=True)
                return
            try:
                yk = YooKassaClient(config.yookassa)
                bot_info = await bot.get_me()
                payment_data = yk.create_payment(
                    amount=price / 100.0,
                    description=f"Bypass +{pack['gb_amount']} ГБ",
                    return_url=f"https://t.me/{bot_info.username}?start=payment_success",
                    metadata={
                        "user_id": user_id,
                        "product_type": "bypass_pack",
                        "pack_id": pack_id,
                        "payment_source": "bot",
                    },
                )
                async with get_connection() as conn:
                    await conn.execute(
                        """
                        INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        user_id, price, "RUB", f"bypass_pack:{pack_id}",
                        "bypass_pack", "pending", payment_data["id"],
                    )
                b = InlineKeyboardBuilder()
                b.row(btn("Оплатить", "card", url=payment_data["confirmation_url"]))
                b.row(btn("Назад", "back", callback_data=f"bypass_pack_choose:{pack_id}"))
                await callback.message.edit_text(
                    f"{E.card} <b>Оплата</b>\n\n+{pack['gb_amount']} ГБ — {format_price_rub(price)}\n\n"
                    f"Нажмите кнопку для перехода к оплате.",
                    parse_mode="HTML",
                    reply_markup=b.as_markup(),
                )
                await callback.answer()
            except Exception as e:
                logger.error("bypass pack yookassa: %s", e, exc_info=True)
                await callback.answer(f"{E.error} Ошибка", show_alert=True)
