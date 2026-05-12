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
from .subscriptions import handle_expired_subscriptions, send_upcoming_subscription_reminders
from .tier_autopay import run_yookassa_autopay_renewals
from .engagement_notifications import run_engagement_notifications
from .webhook_server import WebhookServer
from .yookassa_client import YooKassaClient
from .flyer_client import FlyerClient
from .payments import process_webhook_payment
from .plans import get_subscription_plans, get_renewal_plans, PAYMENT_METHODS
from .traffic_worker import run_traffic_sync_loop

# Импортируем обработчики
from .handlers import start, subscription, payment, admin
from .handlers.tiers import setup_tier_handlers
from .bypass_notifications import check_bypass_traffic_notifications

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Глобальные переменные
config = load_config()
bot = Bot(token=config.bot.bot_token)
dp = Dispatcher()
scheduler: AsyncIOScheduler | None = None
webhook_server: WebhookServer | None = None
traffic_worker_task: asyncio.Task | None = None
_shutdown_in_progress = False


def setup_scheduler():
    """Инициализация и запуск планировщика задач"""
    global scheduler
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    
    # Проверка истекших подписок (каждый день в 00:05)
    scheduler.add_job(
        handle_expired_subscriptions,
        'cron',
        hour=0,
        minute=5,
        args=[bot]
    )
    
    # Проверка предстоящих окончаний подписки (каждый день в 13:00)
    scheduler.add_job(
        send_upcoming_subscription_reminders,
        'cron',
        hour=13,
        minute=0,
        args=[bot, config]
    )
    
    # Проверка bypass уведомлений (каждые 5 минут)
    scheduler.add_job(
        check_bypass_traffic_notifications,
        'interval',
        minutes=5,
        args=[bot],
    )

    scheduler.add_job(
        run_yookassa_autopay_renewals,
        "cron",
        hour=10,
        minute=15,
        args=[config],
    )

    # Engagement notifications (daily at 11:00 and 18:00)
    scheduler.add_job(
        run_engagement_notifications,
        "cron",
        hour=11,
        minute=0,
        args=[bot, config],
    )
    scheduler.add_job(
        run_engagement_notifications,
        "cron",
        hour=18,
        minute=0,
        args=[bot, config],
    )

    # Также запускаем при старте
    moscow_tz = pytz.timezone("Europe/Moscow")
    now_moscow = datetime.now(moscow_tz)

    # (через 30 секунд)
    scheduler.add_job(
        handle_expired_subscriptions,
        'date',
        run_date=now_moscow + timedelta(seconds=30),
        args=[bot],
        misfire_grace_time=3600
    )
    
    # (через 60 секунд)
    scheduler.add_job(
        send_upcoming_subscription_reminders,
        'date',
        run_date=now_moscow + timedelta(seconds=60),
        args=[bot, config],
        misfire_grace_time=3600
    )
    
    scheduler.start()
    logger.info("APScheduler started")


async def create_webhook_payment_processor():
    """Создаёт функцию-обработчик платежей для webhook сервера"""
    async def processor(payment_id: str, payment_obj: dict, metadata: dict):
        subscription_plans = await get_subscription_plans()
        renewal_plans = await get_renewal_plans()
        await process_webhook_payment(
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
    
    # Останавливаем фоновый воркер трафика
    global traffic_worker_task
    if traffic_worker_task and not traffic_worker_task.done():
        try:
            traffic_worker_task.cancel()
            try:
                await traffic_worker_task
            except (asyncio.CancelledError, Exception):
                pass
            logger.info("Traffic worker stopped")
        except Exception as e:
            logger.error(f"Error stopping traffic worker: {e}")

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
    
    # Миграция конфигураций (удаление спецсимволов из ID для стабильности в браузерах)
    try:
        from .subscriptions import migrate_all_vless_configs
        await migrate_all_vless_configs()
    except Exception as e:
        logger.error(f"Error during VLESS configuration migration: {e}")
    
    # Настраиваем обработчики
    await start.setup_start_handler(dp, bot, config)
    await subscription.setup_subscription_handlers(dp, bot, config)
    await setup_tier_handlers(dp, bot, config)
    await payment.setup_payment_handlers(dp, bot, config)
    await start.setup_other_handlers(dp, bot, config)
    await admin.setup_admin_handlers(dp, bot, config)
    logger.info("All handlers registered")
    
    # Запускаем планировщик
    setup_scheduler()
    
    # Запускаем вебхук сервер
    global webhook_server
    yookassa_client = YooKassaClient(config.yookassa) if config.yookassa.enabled else None
    payment_processor = await create_webhook_payment_processor()
    
    webhook_server = WebhookServer(
        flyer_config=config.flyer,
        yookassa_config=config.yookassa if config.yookassa.enabled else None,
        cryptopay_config=config.cryptopay,
        bot_instance=bot,
        yookassa_client=yookassa_client,
        payment_processor=payment_processor,
        admin_ids=config.bot.admin_ids,
        bot_public_username=config.bot.public_username,
        subscription_public_base_url=config.subscription_base_url,
    )
    
    logger.info(f"Payment methods: YooKassa={'ENABLED' if config.yookassa.enabled else 'DISABLED'}, CryptoPay={'ENABLED' if config.cryptopay.enabled else 'DISABLED'} (testnet={config.cryptopay.testnet})")
    if config.cryptopay.enabled and config.cryptopay.api_token:
        logger.info(f"CryptoPay Token loaded: {config.cryptopay.api_token[:4]}...{config.cryptopay.api_token[-4:]}")
    elif config.cryptopay.enabled:
        logger.error("⚠️ CryptoPay is ENABLED but CRYPTOPAY_API_TOKEN is MISSING!")
    
    # Фоновый воркер учёта трафика: раз в N секунд опрашивает панели X-UI,
    # пишет traffic_lifetime_bytes в vpn_keys и агрегирует users.traffic_used_bytes.
    global traffic_worker_task
    try:
        traffic_worker_task = asyncio.create_task(run_traffic_sync_loop())
        logger.info("Traffic sync worker scheduled")
    except Exception as e:
        logger.error(f"Failed to start traffic worker: {e}")

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
