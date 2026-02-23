"""
Главный модуль бота - точка входа
"""
import os
import asyncio
import signal
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

from .config import load_config
from .database import init_db
from .subscriptions import handle_expired_subscriptions
from .webhook_server import WebhookServer
from .yookassa_client import YooKassaClient
from .flyer_client import FlyerClient
from .payments import process_yookassa_payment
from .plans import get_subscription_plans, get_renewal_plans, PAYMENT_METHODS

# Импортируем обработчики
from .handlers import start, subscription, payment

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Глобальные переменные
config = load_config()
bot = Bot(token=config.bot.bot_token)
dp = Dispatcher()
scheduler: AsyncIOScheduler | None = None
webhook_server: WebhookServer | None = None
_shutdown_in_progress = False


def setup_scheduler():
    """Инициализация и запуск планировщика задач"""
    global scheduler
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    
    # Проверка истекших подписок (каждый день в 00:05)
    scheduler.add_job(
        lambda: handle_expired_subscriptions(bot),
        'cron',
        hour=0,
        minute=5
    )
    
    # Также запускаем при старте (через 30 секунд)
    scheduler.add_job(
        lambda: handle_expired_subscriptions(bot),
        'date',
        run_date=datetime.now() + timedelta(seconds=30)
    )
    
    scheduler.start()
    logger.info("APScheduler started")


async def create_yookassa_payment_processor():
    """Создаёт функцию-обработчик платежей YooKassa для webhook сервера"""
    async def processor(payment_id: str, payment_obj: dict, metadata: dict):
        subscription_plans = await get_subscription_plans()
        renewal_plans = await get_renewal_plans()
        await process_yookassa_payment(
            payment_id=payment_id,
            payment_obj=payment_obj,
            metadata=metadata,
            bot=bot,
            config=config,
            subscription_plans=subscription_plans,
            renewal_plans=renewal_plans,
            payment_methods=PAYMENT_METHODS
        )
    return processor


async def shutdown():
    """Корректное завершение всех компонентов бота"""
    global _shutdown_in_progress
    
    if _shutdown_in_progress:
        return
    
    _shutdown_in_progress = True
    logger.info("Shutting down gracefully...")
    
    # Останавливаем polling
    try:
        await dp.stop_polling()
        logger.info("Polling stopped")
    except Exception as e:
        logger.error(f"Error stopping polling: {e}")
    
    # Останавливаем scheduler
    global scheduler
    if scheduler and scheduler.running:
        try:
            scheduler.shutdown(wait=True)
            logger.info("Scheduler stopped")
        except Exception as e:
            logger.error(f"Error stopping scheduler: {e}")
    
    # Останавливаем вебхук сервер
    global webhook_server
    if webhook_server:
        try:
            await webhook_server.stop()
            logger.info("Webhook server stopped")
        except Exception as e:
            logger.error(f"Error stopping webhook server: {e}")
    
    # Закрываем Flyer клиент
    if config.flyer.enabled:
        try:
            flyer_client = FlyerClient(config.flyer)
            await flyer_client.close()
            logger.info("Flyer client closed")
        except Exception as e:
            logger.error(f"Error closing Flyer client: {e}")
    
    # Закрываем сессию бота
    try:
        await bot.session.close()
        logger.info("Bot session closed")
    except Exception as e:
        logger.error(f"Error closing bot session: {e}")


async def main():
    """Основная функция запуска бота"""
    # Устанавливаем обработчики сигналов
    loop = asyncio.get_running_loop()
    shutdown_task = None
    
    def handle_signal(sig):
        """Синхронный обработчик сигнала"""
        nonlocal shutdown_task
        logger.info(f"Received signal {sig}, initiating shutdown...")
        if shutdown_task is None or shutdown_task.done():
            shutdown_task = asyncio.create_task(shutdown())
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))
        except (ValueError, RuntimeError) as e:
            logger.warning(f"Could not add signal handler for {sig}: {e}")
    
    # Инициализируем БД
    await init_db()
    logger.info("Database initialized")
    
    # Настраиваем обработчики
    await start.setup_start_handler(dp, bot, config)
    await subscription.setup_subscription_handlers(dp, bot, config)
    await payment.setup_payment_handlers(dp, bot, config)
    await start.setup_other_handlers(dp, bot, config)
    logger.info("All handlers registered")
    
    # Запускаем планировщик
    setup_scheduler()
    
    # Запускаем вебхук сервер
    global webhook_server
    yookassa_client = YooKassaClient(config.yookassa) if config.yookassa.enabled else None
    payment_processor = await create_yookassa_payment_processor() if config.yookassa.enabled else None
    
    webhook_server = WebhookServer(
        flyer_config=config.flyer,
        yookassa_config=config.yookassa if config.yookassa.enabled else None,
        bot_instance=bot,
        yookassa_client=yookassa_client,
        payment_processor=payment_processor
    )
    
    webhook_task = None
    if webhook_server:
        try:
            webhook_host = os.getenv("WEBHOOK_HOST", "0.0.0.0")
            webhook_port = int(os.getenv("WEBHOOK_PORT", "8080"))
            webhook_task = asyncio.create_task(
                webhook_server.run(host=webhook_host, port=webhook_port)
            )
            logger.info(f"Webhook server starting on {webhook_host}:{webhook_port}")
            logger.info(f"Subscription endpoint will be available at: {config.subscription_base_url or 'NOT SET'}/sub/{{token}}")
            if not config.subscription_base_url:
                logger.error("⚠️  WARNING: SUBSCRIPTION_BASE_URL not set in .env!")
                logger.error("⚠️  Please set SUBSCRIPTION_BASE_URL=https://your-domain.com in .env file")
        except Exception as e:
            logger.error(f"Error starting webhook server: {e}")
    
    try:
        logger.info("Bot is starting...")
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except asyncio.CancelledError:
        logger.info("Polling was cancelled")
    except Exception as e:
        logger.error(f"Error in polling: {e}", exc_info=True)
    finally:
        if shutdown_task is None or shutdown_task.done():
            await shutdown()
        else:
            await shutdown_task


if __name__ == "__main__":
    print("Бот запущен!")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        print("\nБот остановлен!")
