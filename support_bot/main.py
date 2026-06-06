"""
Точка входа бота техподдержки SvoyVPN.
Запуск из корня проекта: python -m support_bot.main
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Корень репозитория в PYTHONPATH для импорта bot.*
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.database import init_db
from support_bot.ai_service import SupportAIService
from support_bot.config import load_support_config
from support_bot.db import init_support_tables
from support_bot.promo_offers import init_promo_tables
from support_bot.handlers import setup_staff_handlers, setup_user_handlers

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_support_config()
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    ai = SupportAIService(config)

    await init_db()
    await init_support_tables()
    await init_promo_tables()

    setup_staff_handlers(dp, bot, config, ai)
    setup_user_handlers(dp, bot, config, ai)

    logger.info("Support bot starting (staff_ids=%s)", config.staff_ids)
    try:
        await dp.start_polling(bot)
    finally:
        await ai.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
