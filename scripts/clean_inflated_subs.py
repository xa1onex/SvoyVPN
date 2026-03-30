
import asyncio
import os
import sys
import logging
from datetime import datetime, timedelta
import pytz
from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Добавляем путь к проекту в sys.path
sys.path.append(os.getcwd())

try:
    from bot.database import get_connection, get_pool
    from bot.config import load_config
    from bot.subscriptions import sync_user_keys
    from bot.plans import get_renewal_plans, format_price_both
except ImportError:
    logger.error("Не удалось импортировать модули бота. Запустите скрипт из корня проекта.")
    sys.exit(1)

async def process_inflated_subscriptions():
    """
    Находит накрученные подписки, уменьшает их до 1 дня и отправляет СТАНДАРТНОЕ уведомление бота.
    """
    try:
        config = load_config()
    except Exception as e:
        logger.error(f"Ошибка загрузки конфига: {e}")
        return

    bot = Bot(token=config.bot.bot_token)
    now_moscow = datetime.now(pytz.timezone("Europe/Moscow"))
    new_end_date = (now_moscow + timedelta(days=1)).date()
    end_date_str = new_end_date.strftime("%d.%m.%Y")
    
    async with get_connection() as conn:
        # 1. Поиск "накрученных" пользователей
        query = """
            SELECT u.user_id, u.username, u.first_name, u.subscription_end
            FROM users u
            LEFT JOIN payments p ON u.user_id = p.user_id AND p.status = 'completed'
            WHERE u.pay_subscribed = TRUE 
              AND u.subscription_end >= CURRENT_DATE
              AND p.id IS NULL
        """
        
        inflated_users = await conn.fetch(query)
        
        if not inflated_users:
            logger.info("Накрученных подписок не обнаружено.")
            return

        print("\n" + "="*60)
        print(f"НАЙДЕНО ПОЛЬЗОВАТЕЛЕЙ ДЛЯ ОЧИСТКИ: {len(inflated_users)}")
        print("-" * 60)
        for user in inflated_users:
            uname = f"(@{user['username']})" if user['username'] else "(без username)"
            print(f"ID: {user['user_id']} | Name: {user['first_name']} {uname}")
        print("="*60 + "\n")

        confirm = input(f"Сократить подписку до 1 дня и отправить СТАНДАРТНОЕ уведомление? (y/n): ")
        if confirm.lower() != 'y':
            print("Отменено.")
            return

        # Подготовка стандартных кнопок продления
        renewal_plans = await get_renewal_plans()
        builder_template = InlineKeyboardBuilder()
        for plan_id, plan_data in renewal_plans.items():
            builder_template.button(
                text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                callback_data=f"plan:{plan_id}"
            )
        builder_template.adjust(1)
        builder_template.row(
            InlineKeyboardButton(text="💎 Все тарифы", callback_data="open_premium"),
            InlineKeyboardButton(text="🎁 Бесплатно", callback_data="open_invite")
        )

        processed_count = 0
        error_count = 0
        
        print("\nЗапуск процесса...")
        
        for user in inflated_users:
            user_id = user['user_id']
            username = user['username'] or "none"
            
            try:
                # 2. Обновляем БД (срок до завтра)
                await conn.execute(
                    "UPDATE users SET subscription_end = $1 WHERE user_id = $2",
                    new_end_date, user_id
                )
                
                # Помечаем, что напоминание '1_day' уже отправлено
                await conn.execute('''
                    INSERT INTO subscription_reminders (user_id, reminder_type) 
                    VALUES ($1, $2)
                    ON CONFLICT (user_id, reminder_type) DO NOTHING
                ''', user_id, '1_day')
                
                # Синхронизируем VPN (обязательно)
                try:
                    await sync_user_keys(user_id)
                except Exception as e:
                    logger.warning(f"Ошибка синхронизации ключей для {user_id}: {e}")

                # 3. СТАНДАРТНОЕ УВЕДОМЛЕНИЕ БОТА
                text = (
                    f"⏰ <b>Внимание! Подписка почти закончилась</b>\n\n"
                    f"Ваша подписка на VPN истекает <b>ЗАВТРА</b> ({end_date_str}).\n\n"
                    f"Чтобы интернет не отключился в самый подходящий момент, "
                    f"рекомендуем продлить её прямо сейчас по выгодной цене! 🚀"
                )
                
                try:
                    await bot.send_message(
                        user_id, 
                        text, 
                        reply_markup=builder_template.as_markup(), 
                        parse_mode="HTML"
                    )
                    print(f"✅ Готово: {user_id} (@{username})")
                except Exception as e:
                    print(f"⚠️ Ошибка отправки {user_id}: {e}")
                
                processed_count += 1
                await asyncio.sleep(0.05)
                
            except Exception as e:
                logger.error(f"Критическая ошибка для {user_id}: {e}")
                error_count += 1

        print("\n" + "="*60)
        print(f"ИТОГИ:")
        print(f"Почищено пользователей: {processed_count}")
        print(f"Ошибок:                 {error_count}")
        print("="*60 + "\n")
    
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(process_inflated_subscriptions())
