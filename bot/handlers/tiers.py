"""
Обработчики новой системы тарифов (Lite / Standard / Pro).
Покупка, апгрейд, докупка bypass ГБ.
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
    PAYMENT_METHODS,
    TIER_ORDER,
    TIERS,
    calculate_upgrade_price,
    can_upgrade,
    format_price_both,
    format_price_rub,
    format_price_stars,
    get_bypass_packs,
    get_tier_bypass_gb,
    get_tier_max_devices,
    get_tier_plans,
    get_tier_plans_for_tier,
)
from ..traffic import user_bypass_traffic_snapshot
from ..yookassa_client import YooKassaClient

logger = logging.getLogger(__name__)


async def build_tiers_message(user_id: int):
    """Build the tier selection text and markup (standalone, importable)."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT subscription_tier, pay_subscribed, subscription_end
            FROM users WHERE user_id = $1
            """,
            user_id,
        )

    current_tier = (row["subscription_tier"] if row else None) or "none"
    is_active = (
        row
        and row["pay_subscribed"]
        and row["subscription_end"]
        and row["subscription_end"].date() >= datetime.now().date()
    ) if row else False

    text_parts = ["🚀 <b>Подписка VPN</b>\n"]
    if is_active and current_tier != "legacy" and current_tier != "none":
        tier_info = TIERS.get(current_tier)
        if tier_info:
            text_parts.append(
                f"Текущий тариф: <b>{tier_info['name']}</b>\n"
            )
    elif is_active and current_tier == "legacy":
        text_parts.append("Текущий тариф: <b>Legacy</b> (старая подписка)\n")

    text_parts.append("")
    text_parts.append("Обычный VPN — <b>безлимитный</b> на всех тарифах.")
    text_parts.append("Bypass-сервера — для обхода блокировок (лимит ГБ/мес).\n")

    for tier_id in TIER_ORDER:
        t = TIERS[tier_id]
        marker = " ← ваш" if current_tier == tier_id else ""
        text_parts.append(f"<b>{t['name']}</b>{marker}")
        for f in t["features"]:
            text_parts.append(f"  • {f}")
        text_parts.append("")

    text = "\n".join(text_parts)

    builder = InlineKeyboardBuilder()
    for tier_id in TIER_ORDER:
        t = TIERS[tier_id]
        if is_active and current_tier == tier_id:
            builder.row(
                InlineKeyboardButton(
                    text=f"✅ {t['name']} (текущий)",
                    callback_data=f"tier_info:{tier_id}",
                )
            )
        elif is_active and can_upgrade(current_tier, tier_id):
            builder.row(
                InlineKeyboardButton(
                    text=f"⬆️ {t['name']} (апгрейд)",
                    callback_data=f"tier_upgrade:{tier_id}",
                )
            )
        else:
            builder.row(
                InlineKeyboardButton(
                    text=f"💎 {t['name']}",
                    callback_data=f"tier_select:{tier_id}",
                )
            )

    if is_active:
        builder.row(
            InlineKeyboardButton(
                text="📶 Докупить bypass ГБ",
                callback_data="open_bypass_packs",
            )
        )

    builder.row(
        InlineKeyboardButton(text="◀️ Назад", callback_data="go_back_subscription")
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
        """Описание тарифа + сразу создание платежа и кнопка оплаты."""
        tier_id = callback.data.split(":")[1]
        if tier_id not in TIERS:
            await callback.answer("❌ Тариф не найден", show_alert=True)
            return

        t = TIERS[tier_id]
        plans = await get_tier_plans_for_tier(tier_id)
        if not plans:
            await callback.answer("❌ Нет доступных планов", show_alert=True)
            return

        plan_id, plan_data = next(iter(plans.items()))
        await _do_tier_pay(callback, plan_id)

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
            await callback.answer("❌ План не найден", show_alert=True)
            return

        plan = plans[plan_id]
        tier_id = plan["tier"]
        t = TIERS.get(tier_id, {})

        if not config.yookassa.enabled:
            await callback.answer("❌ ЮKassa не настроена", show_alert=True)
            return
        price = plan["price_rub"]
        if price < 100:
            await callback.answer("❌ Минимальная сумма — 1₽", show_alert=True)
            return
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

            text = f"💎 <b>{t.get('name', tier_id)}</b> · {format_price_rub(price)}/мес\n\n"
            for feat in t.get("features", []):
                text += f"• {feat}\n"

            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_data["confirmation_url"]))
            b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="open_tiers"))
            await callback.message.edit_text(
                text, parse_mode="HTML", reply_markup=b.as_markup()
            )
            await callback.answer()
        except Exception as e:
            logger.error("tier yookassa error: %s", e, exc_info=True)
            await callback.answer("❌ Ошибка создания платежа", show_alert=True)

    @dp.callback_query(F.data.startswith("tier_pay:"))
    async def handle_tier_pay(callback: CallbackQuery):
        """Router for tier_pay: callbacks."""
        parts = callback.data.split(":")
        if len(parts) < 2:
            await callback.answer("❌ Ошибка данных", show_alert=True)
            return
        plan_id = parts[1]
        await _do_tier_pay(callback, plan_id)

    # ------------------------------------------------------------------
    # Tier info (current tier details)
    # ------------------------------------------------------------------
    @dp.callback_query(F.data.startswith("tier_info:"))
    async def handle_tier_info(callback: CallbackQuery):
        """Show current tier details with bypass usage."""
        user_id = callback.from_user.id

        async with get_connection() as conn:
            snap = await user_bypass_traffic_snapshot(conn, user_id)
            row = await conn.fetchrow(
                "SELECT subscription_end FROM users WHERE user_id = $1", user_id
            )

        tier_id = snap["tier"]
        tier_info = TIERS.get(tier_id, {})

        end_str = "—"
        if row and row["subscription_end"]:
            end_str = row["subscription_end"].strftime("%d.%m.%Y")

        used_gb = snap["bypassUsedGb"]
        limit_gb = snap["bypassLimitGb"]
        remaining_gb = snap["bypassRemainingGb"]
        percent = snap["bypassPercentUsed"]

        text = (
            f"📊 <b>Ваш тариф: {tier_info.get('name', tier_id)}</b>\n\n"
            f"📅 Подписка до: <b>{end_str}</b>\n\n"
            f"🔓 <b>Bypass трафик:</b>\n"
            f"  Использовано: {used_gb:.1f} / {limit_gb:.0f} ГБ ({percent:.0f}%)\n"
            f"  Осталось: <b>{remaining_gb:.1f} ГБ</b>\n"
        )
        if snap["bypassBonusGb"] > 0:
            text += f"  Бонус (докупка): +{snap['bypassBonusGb']} ГБ\n"
        text += f"\n🌐 Обычный VPN: <b>безлимит</b>\n"
        text += f"📱 Устройств: до {tier_info.get('max_devices', '?')}\n"

        if snap["bypassExceeded"]:
            text += "\n⚠️ <b>Bypass лимит исчерпан!</b> Докупите ГБ или повысьте тариф.\n"

        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="📶 Докупить bypass ГБ",
                callback_data="open_bypass_packs",
            )
        )
        # Show upgrade button if possible
        if tier_id in TIER_ORDER:
            idx = TIER_ORDER.index(tier_id)
            if idx < len(TIER_ORDER) - 1:
                next_tier = TIER_ORDER[idx + 1]
                next_name = TIERS[next_tier]["name"]
                builder.row(
                    InlineKeyboardButton(
                        text=f"⬆️ Повысить до {next_name}",
                        callback_data=f"tier_upgrade:{next_tier}",
                    )
                )
        builder.row(
            InlineKeyboardButton(text="◀️ К тарифам", callback_data="open_tiers")
        )

        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=builder.as_markup()
        )
        await callback.answer()

    # ------------------------------------------------------------------
    # Tier upgrade
    # ------------------------------------------------------------------
    @dp.callback_query(F.data.startswith("tier_upgrade:"))
    async def handle_tier_upgrade(callback: CallbackQuery):
        """Show upgrade options to a higher tier."""
        target_tier = callback.data.split(":")[1]
        user_id = callback.from_user.id

        if target_tier not in TIERS:
            await callback.answer("❌ Тариф не найден", show_alert=True)
            return

        t = TIERS[target_tier]
        plans = await get_tier_plans_for_tier(target_tier)

        async with get_connection() as conn:
            row = await conn.fetchrow(
                "SELECT subscription_tier, tier_duration_months FROM users WHERE user_id = $1",
                user_id,
            )

        current_tier = (row["subscription_tier"] if row else None) or "none"
        current_duration = (row["tier_duration_months"] if row else None) or 1
        current_name = TIERS.get(current_tier, {}).get("name", current_tier)

        text = (
            f"⬆️ <b>Апгрейд: {current_name} → {t['name']}</b>\n\n"
            f"Что получите:\n"
        )
        for feat in t["features"]:
            text += f"  • {feat}\n"
        text += (
            f"\n💡 Срок подписки НЕ меняется.\n"
            f"Bypass-лимит обновляется сразу.\n"
            f"Оплата = разница между тарифами.\n\n"
            f"Оформление апгрейда:"
        )

        builder = InlineKeyboardBuilder()
        for plan_id, plan_data in plans.items():
            result = await calculate_upgrade_price(
                user_id, target_tier, plan_data["duration"]
            )
            if result["valid"]:
                diff_text = format_price_both(result["price_rub"], result["price_stars"])
                builder.row(
                    InlineKeyboardButton(
                        text=f"{plan_data['title']} — доплата {diff_text}",
                        callback_data=f"tier_upgrade_pay:{plan_id}",
                    )
                )
            else:
                builder.row(
                    InlineKeyboardButton(
                        text=f"{plan_data['title']} — {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                        callback_data=f"tier_buy:{plan_id}",
                    )
                )

        builder.row(
            InlineKeyboardButton(text="◀️ К тарифам", callback_data="open_tiers")
        )

        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=builder.as_markup()
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("tier_upgrade_pay:"))
    async def handle_tier_upgrade_pay(callback: CallbackQuery):
        """Redirect upgrade payment directly to tier_upgrade_do."""
        plan_id = callback.data.split(":")[1]
        await _do_tier_upgrade(callback, plan_id)

    async def _do_tier_upgrade(callback: CallbackQuery, plan_id: str):
        """Create YooKassa payment for tier upgrade with save_payment_method."""
        user_id = callback.from_user.id

        plans = await get_tier_plans()
        if plan_id not in plans:
            await callback.answer("❌ План не найден", show_alert=True)
            return

        plan = plans[plan_id]
        result = await calculate_upgrade_price(user_id, plan["tier"], plan["duration"])
        if not result["valid"]:
            await callback.answer(f"❌ {result['reason']}", show_alert=True)
            return

        if not config.yookassa.enabled:
            await callback.answer("❌ ЮKassa не настроена", show_alert=True)
            return

        price_rub = result["price_rub"]
        if price_rub < 100:
            await callback.answer("❌ Минимальная сумма 1₽", show_alert=True)
            return
        try:
            yk = YooKassaClient(config.yookassa)
            bot_info = await bot.get_me()
            payment_data = yk.create_payment(
                amount=price_rub / 100.0,
                description=f"Апгрейд VPN → {plan['title']}",
                return_url=f"https://t.me/{bot_info.username}?start=payment_success",
                metadata={
                    "user_id": str(user_id),
                    "plan_id": plan_id,
                    "method_id": "yookassa",
                    "product_type": "tier_upgrade",
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
                    user_id, price_rub, "RUB", plan_id, "tier_upgrade", "pending",
                    payment_data["id"],
                )
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(text="💳 Оплатить", url=payment_data["confirmation_url"]))
            b.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"tier_upgrade:{plan['tier']}"))
            await callback.message.edit_text(
                f"💳 <b>Апгрейд → {plan['title']}</b>\n\n"
                f"Доплата: <b>{format_price_rub(price_rub)}</b>",
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            await callback.answer()
        except Exception as e:
            logger.error("upgrade yookassa: %s", e, exc_info=True)
            await callback.answer("❌ Ошибка", show_alert=True)

    @dp.callback_query(F.data.startswith("tier_upgrade_do:"))
    async def handle_tier_upgrade_do(callback: CallbackQuery):
        """Router for tier_upgrade_do: callbacks."""
        parts = callback.data.split(":")
        if len(parts) < 2:
            await callback.answer("❌ Ошибка", show_alert=True)
            return
        plan_id = parts[1]
        await _do_tier_upgrade(callback, plan_id)

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
                "📶 <b>Докупка bypass трафика</b>\n\n"
                "Доступно только при <b>активной подписке</b>.\n"
                "Оформите подписку в разделе «Подписка»."
            )
            b = InlineKeyboardBuilder()
            b.row(InlineKeyboardButton(text="💎 Выбрать тариф", callback_data="open_tiers"))
            b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="go_back_subscription"))
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
            await callback.answer()
            return

        remaining = snap["bypassRemainingGb"]
        limit = snap["bypassLimitGb"]
        used = snap["bypassUsedGb"]

        text = (
            f"📶 <b>Докупить bypass трафик</b>\n\n"
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
                        text=f"+{p['gb_amount']} ГБ — {format_price_both(p['price_rub'], p['price_stars'])}",
                        callback_data=f"bypass_pack_choose:{p['id']}",
                    )
                )

        # Suggest upgrade if applicable
        tier = snap.get("tier", "none")
        if tier in TIER_ORDER:
            idx = TIER_ORDER.index(tier)
            if idx < len(TIER_ORDER) - 1:
                next_tier = TIER_ORDER[idx + 1]
                next_name = TIERS[next_tier]["name"]
                next_gb = get_tier_bypass_gb(next_tier)
                builder.row(
                    InlineKeyboardButton(
                        text=f"⬆️ Апгрейд до {next_name} ({next_gb} ГБ/мес)",
                        callback_data=f"tier_upgrade:{next_tier}",
                    )
                )

        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="open_tiers"))
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("bypass_pack_choose:"))
    async def handle_bypass_pack_choose(callback: CallbackQuery):
        """Choose payment method for bypass pack."""
        user_id = callback.from_user.id
        try:
            pack_id = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await callback.answer("❌ Ошибка", show_alert=True)
            return

        async with get_connection() as conn:
            pack = await conn.fetchrow(
                "SELECT id, title, gb_amount, price_rub, price_stars FROM bypass_pack_products WHERE id = $1 AND is_active = TRUE",
                pack_id,
            )
        if not pack:
            await callback.answer("❌ Пакет недоступен", show_alert=True)
            return

        text = (
            f"📶 <b>Оплата: +{pack['gb_amount']} ГБ bypass</b>\n\n"
            f"{pack['title']}\n\n"
            f"Выберите способ оплаты:"
        )
        b = InlineKeyboardBuilder()
        if int(pack["price_stars"] or 0) >= 1:
            b.row(
                InlineKeyboardButton(
                    text=f"⭐ Stars ({format_price_stars(pack['price_stars'])})",
                    callback_data=f"bypass_pack_pay:{pack_id}:stars",
                )
            )
        if config.yookassa.enabled and int(pack["price_rub"] or 0) >= 100:
            b.row(
                InlineKeyboardButton(
                    text=f"💳 Карта ({format_price_rub(pack['price_rub'])})",
                    callback_data=f"bypass_pack_pay:{pack_id}:yookassa",
                )
            )
        b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="open_bypass_packs"))
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("bypass_pack_pay:"))
    async def handle_bypass_pack_pay(callback: CallbackQuery):
        """Process bypass pack payment."""
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer("❌ Ошибка", show_alert=True)
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
            await callback.answer("❌ Пакет недоступен", show_alert=True)
            return

        if method_id == "stars":
            price = int(pack["price_stars"] or 0)
            if price < 1:
                await callback.answer("❌ Неверная цена", show_alert=True)
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
                await callback.answer("❌ Ошибка создания счёта", show_alert=True)

        elif method_id == "yookassa":
            price = int(pack["price_rub"] or 0)
            if price < 100:
                await callback.answer("❌ Минимум 1₽", show_alert=True)
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
                b.row(InlineKeyboardButton(text="💳 Оплатить", url=payment_data["confirmation_url"]))
                b.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"bypass_pack_choose:{pack_id}"))
                await callback.message.edit_text(
                    f"💳 <b>Оплата</b>\n\n+{pack['gb_amount']} ГБ — {format_price_rub(price)}\n\n"
                    f"Нажмите кнопку для перехода к оплате.",
                    parse_mode="HTML",
                    reply_markup=b.as_markup(),
                )
                await callback.answer()
            except Exception as e:
                logger.error("bypass pack yookassa: %s", e, exc_info=True)
                await callback.answer("❌ Ошибка", show_alert=True)
