
import asyncio
import os
import sys
import logging
from datetime import datetime, timedelta
import pytz
from aiogram import Bot

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Добавляем путь к проекту в sys.path
sys.path.append(os.getcwd())

try:
    from bot.database import get_connection, get_pool
    from bot.config import load_config
    from bot.subscriptions import sync_user_keys
except ImportError:
    logger.error("Не удалось импортировать модули бота. Запустите скрипт из корня проекта.")
    sys.exit(1)

async def process_inflated_subscriptions():
    """
    Находит накрученные подписки, уменьшает их до 1 дня и уведомляет пользователей.
    Накрученными считаются активные подписки (pay_subscribed=True) без успешных платежей.
    """
    try:
        config = load_config()
    except Exception as e:
        logger.error(f"Ошибка загрузки конфига: {e}")
        logger.info("Убедитесь, что переменные окружения (BOT_TOKEN, DATABASE_URL и др.) заданы.")
        return

    bot = Bot(token=config.bot.bot_token)
    now_moscow = datetime.now(pytz.timezone("Europe/Moscow"))
    # Устанавливаем дату окончания на "завтра в 23:59"
    new_end_date = (now_moscow + timedelta(days=1)).date()
    
    async with get_connection() as conn:
        # 1. Поиск "накрученных" пользователей
        # Критерий: есть активная подписка, но нет записей в payments с состоянием 'completed'
        query = """
            SELECT u.user_id, u.first_name, u.subscription_end
            FROM users u
            LEFT JOIN payments p ON u.user_id = p.user_id AND p.status = 'completed'
            WHERE u.pay_subscribed = TRUE 
              AND u.subscription_end >= CURRENT_DATE
              AND p.id IS NULL
        """
        
        inflated_users = await conn.fetch(query)
        logger.info(f"Найдено потенциально накрученных пользователей: {len(inflated_users)}")
        
        if not inflated_users:
            logger.info("Накрученных подписок не обнаружено.")
            return

        processed_count = 0
        error_count = 0
        
        for user in inflated_users:
            user_id = user['user_id']
            first_name = user['first_name'] or "Пользователь"
            
            try:
                # 2. Обновляем дату окончания подписки в БД
                await conn.execute(
                    "UPDATE users SET subscription_end = $1 WHERE user_id = $2",
                    new_end_date, user_id
                )
                
                # Синхронизируем ключи на VPN серверах (важно, чтобы доступ действительно ограничился)
                try:
                    await sync_user_keys(user_id)
                except Exception as e:
                    logger.warning(f"Ошибка синхронизации ключей для {user_id}: {e}")

                # 3. Отправляем уведомление
                message_text = (
                    f"🎁 <b>{first_name}, ваша подписка активирована!</b>\n\n"
                    f"Мы начислили вам бонусный период доступа к VPN.\n"
                    f"📅 Срок действия: <b>1 день</b>\n"
                    f"🏁 Дата окончания: <b>{new_end_date.strftime('%d.%m.%Y')}</b>\n\n"
                    f"Используйте это время, чтобы оценить скорость и стабильность нашего сервиса! 🚀"
                )
                
                try:
                    await bot.send_message(user_id, message_text, parse_mode="HTML")
                    logger.info(f"✅ Обработан пользователь {user_id} ({first_name})")
                except Exception as e:
                    logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
                
                processed_count += 1
                # Небольшая пауза, чтобы не попасть под лимиты Telegram
                await asyncio.sleep(0.05)
                
            except Exception as e:
                logger.error(f"Ошибка при обработке пользователя {user_id}: {e}")
                error_count += 1

        logger.info(f"Завершено. Успешно обработано: {processed_count}, Ошибок: {error_count}")
    
    await bot.session.close()

if __name__ == "__main__":
    confirm = input("Вы уверены, что хотите уменьшить накрученные подписки до 1 дня? (y/n): ")
    if confirm.lower() == 'y':
        asyncio.run(process_inflated_subscriptions())
    else:
        print("Отменено.")
