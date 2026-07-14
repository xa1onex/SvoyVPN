"""
Раздел «Подарок»: пригласи друга — Plus на n дней, история начислений.
"""
from __future__ import annotations

import logging
from typing import Union

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import AppConfig
from ..database import get_connection
from ..referral import build_referral_context, format_earn_screen, track_referral_page_open
from ..referral_rewards import get_referral_bonus_days
from ..custom_emojis import btn, copy_btn, E

logger = logging.getLogger(__name__)


async def clear_earn_input_state(state: FSMContext) -> None:
    await state.clear()


async def _gift_text_and_keyboard(bot: Bot, actor):
    ref_ctx = await build_referral_context(bot, actor)
    text = format_earn_screen(ref_ctx)

    b = InlineKeyboardBuilder()
    ref_link = ref_ctx.get("ref_link") or ""
    if ref_link:
        b.row(
            btn("Пригласить", "invite", url=ref_ctx["share_url"]),
            copy_btn("Скопировать", "copy", copy_text=ref_link),
        )
    else:
        b.row(btn("Пригласить", "invite", url=ref_ctx["share_url"]))
    b.row(btn("История", "history", callback_data="gift_history:0"))
    b.row(btn("Назад", "back", callback_data="go_back"))
    return text, b.as_markup()


async def render_balance_screen(
    target: Union[Message, CallbackQuery],
    bot: Bot,
    config: AppConfig,
    *,
    track_referral: bool = False,
    state: FSMContext | None = None,
) -> None:
    """Главный экран «Подарок»."""
    if state is not None:
        await clear_earn_input_state(state)
    if isinstance(target, CallbackQuery):
        callback = target
        message = callback.message
        actor = callback.from_user
        is_callback = True
    else:
        callback = None
        message = target
        actor = target.from_user
        is_callback = False

    user_id = actor.id
    if track_referral:
        await track_referral_page_open(user_id)

    text, markup = await _gift_text_and_keyboard(bot, actor)

    if is_callback:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    else:
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )


render_gift_screen = render_balance_screen


async def setup_balance_handlers(dp, bot: Bot, config: AppConfig):
    """Обработчики раздела «Подарок»."""

    @dp.callback_query(F.data.in_({"open_invite", "open_balance"}))
    async def handle_open_balance(callback: CallbackQuery, state: FSMContext):
        await render_balance_screen(callback, bot, config, state=state)
        await callback.answer()

    @dp.callback_query(F.data.startswith("gift_history:"))
    @dp.callback_query(F.data == "balance_history")
    async def handle_gift_history(callback: CallbackQuery, state: FSMContext):
        await clear_earn_input_state(state)
        user_id = callback.from_user.id
        if callback.data == "balance_history":
            offset = 0
        else:
            try:
                offset = int(callback.data.split(":")[1])
            except (IndexError, ValueError):
                offset = 0

        page_size = 10
        bonus_days = await get_referral_bonus_days()

        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT kind, event_at, friend_name, friend_username,
                       reward_days, product_label, is_tg_gift
                FROM (
                    SELECT
                        'friend_reg' AS kind,
                        rir.inviter_reward_at AS event_at,
                        u.first_name AS friend_name,
                        u.username AS friend_username,
                        NULL::int AS reward_days,
                        NULL::text AS product_label,
                        FALSE AS is_tg_gift
                    FROM referral_invite_rewards rir
                    JOIN users u ON u.user_id = rir.invited_user_id
                    WHERE rir.inviter_id = $1
                      AND rir.inviter_reward_at IS NOT NULL

                    UNION ALL

                    SELECT
                        'welcome' AS kind,
                        rir.invited_reward_at AS event_at,
                        NULL AS friend_name,
                        NULL AS friend_username,
                        NULL::int AS reward_days,
                        NULL::text AS product_label,
                        FALSE AS is_tg_gift
                    FROM referral_invite_rewards rir
                    WHERE rir.invited_user_id = $1
                      AND rir.invited_reward_at IS NOT NULL

                    UNION ALL

                    SELECT
                        'purchase' AS kind,
                        rpr.created_at AS event_at,
                        u.first_name AS friend_name,
                        u.username AS friend_username,
                        rpr.reward_days,
                        rpr.product_label,
                        EXISTS (
                            SELECT 1 FROM referral_tg_gift_claims g
                            WHERE g.payment_id = rpr.payment_id
                        ) AS is_tg_gift
                    FROM referral_purchase_rewards rpr
                    JOIN users u ON u.user_id = rpr.payer_user_id
                    WHERE rpr.referrer_id = $1
                ) events
                ORDER BY event_at DESC NULLS LAST
                LIMIT $2 OFFSET $3
                """,
                user_id,
                page_size + 1,
                offset,
            )

        has_more = len(rows) > page_size
        rows = rows[:page_size]

        if not rows:
            await callback.answer("Пока нет начислений", show_alert=True)
            return

        lines = []
        for row in rows:
            dt = row["event_at"].strftime("%d.%m.%Y %H:%M")
            if row["kind"] == "friend_reg":
                name = (row["friend_name"] or "Друг").strip()
                uname = row["friend_username"]
                who = f"@{uname}" if uname else name
                lines.append(
                    f"<code>{dt}</code>\n"
                    f"{E.user} Друг <b>{who}</b> зарегистрировался\n"
                    f"{E.gift} <b>+{bonus_days} дн.</b> SvoyVPN Plus"
                )
            elif row["kind"] == "purchase":
                name = (row["friend_name"] or "Друг").strip()
                uname = row["friend_username"]
                who = f"@{uname}" if uname else name
                product = (row["product_label"] or "оплата").strip()
                days = int(row["reward_days"] or 0)
                gift_line = (
                    f"\n{E.gift} Подарок TG — свяжемся с вами"
                    if row["is_tg_gift"]
                    else ""
                )
                lines.append(
                    f"<code>{dt}</code>\n"
                    f"{E.card} Друг <b>{who}</b>: {product}\n"
                    f"{E.gift} <b>+{days} дн.</b> Plus{gift_line}"
                )
            else:
                lines.append(
                    f"<code>{dt}</code>\n"
                    f"{E.gift} Регистрация по ссылке друга\n"
                    f"<b>+{bonus_days} дн.</b> SvoyVPN Plus"
                )

        text = (
            f"{E.clipboard} <b>История подарков</b>\n"
            f"<i>Сейчас за друга: +{bonus_days} дн. Plus</i>\n\n"
            + "\n\n".join(lines)
        )

        b = InlineKeyboardBuilder()
        nav = []
        if offset > 0:
            nav.append(
                btn("", "back",
                    callback_data=f"gift_history:{max(0, offset - page_size)}",
                )
            )
        if has_more:
            nav.append(
                btn("", "forward",
                    callback_data=f"gift_history:{offset + page_size}",
                )
            )
        if nav:
            b.row(*nav)
        b.row(btn("Назад", "back", callback_data="open_invite"))

        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=b.as_markup()
        )
        await callback.answer()
