
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
    Находит ТОЛЬКО накрученные подписки (без оплат, без рефералов, без триала).
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
        # 1. Поиск РЕАЛЬНО накрученных пользователей
        # Исключаем: тех кто платил, тех кто приглашен, тех кто приглашал, тех кто юзал триал
        query = """
            SELECT u.user_id, u.username, u.first_name, u.subscription_end
            FROM users u
            LEFT JOIN payments p ON u.user_id = p.user_id AND p.status = 'completed'
            WHERE u.pay_subscribed = TRUE 
              AND u.subscription_end >= CURRENT_DATE
              -- Условия честности:
              AND p.id IS NULL                  -- Нет оплат
              AND u.invited_by IS NULL          -- Не приглашен никем (не реферал)
              AND u.referral_count = 0          -- Сам никого не приглашал
              AND u.trial_used = FALSE          -- Не использовал триал
              AND (u.utm_source IS NULL OR u.utm_source != 'migration') -- Не мигрировал из старой базы
        """
        
        inflated_users = await conn.fetch(query)
        
        if not inflated_users:
            print("\n✅ Накрученных подписок не обнаружено. Все активные пользователи имеют на то основания (оплата, рефы или триал).")
            return

        print("\n" + "="*60)
        print(f"НАЙДЕНО ЧИСТО НАКРУЧЕННЫХ ПОЛЬЗОВАТЕЛЕЙ: {len(inflated_users)}")
        print("-" * 60)
        for user in inflated_users:
            uname = f"(@{user['username']})" if user['username'] else "(без username)"
            print(f"ID: {user['user_id']} | Name: {user['first_name']} {uname}")
        print("="*60 + "\n")

        confirm = input(f"Сократить подписку этим {len(inflated_users)} пользователям? (y/n): ")
        if confirm.lower() != 'y':
            print("Отменено.")
            return

        # Подготовка стандартных кнопок
        renewal_plans = await get_renewal_plans()
        builder_template = InlineKeyboardBuilder()
        for plan_id, plan_data in list(renewal_plans.items())[:3]:
            builder_template.button(
                text=f"{plan_data['title']} - {format_price_both(plan_data['price_rub'], plan_data['price_stars'])}",
                callback_data=f"plan:{plan_id}"
            )
        builder_template.adjust(1)
        builder_template.row(
            InlineKeyboardButton(text="💎 Все тарифы", callback_data="open_premium")
        )

        processed_count = 0
        error_count = 0
        
        for user in inflated_users:
            user_id = user['user_id']
            try:
                # Обновляем БД
                await conn.execute("UPDATE users SET subscription_end = $1 WHERE user_id = $2", new_end_date, user_id)
                await conn.execute("INSERT INTO subscription_reminders (user_id, reminder_type) VALUES ($1, '1_day') ON CONFLICT DO NOTHING", user_id)
                
                # Синхронизация VPN
                try: await sync_user_keys(user_id)
                except: pass

                # Стандартное уведомление об окончании
                text = (
                    f"⏰ <b>Внимание! Подписка почти закончилась</b>\n\n"
                    f"Ваша подписка на VPN истекает <b>ЗАВТРА</b> ({end_date_str}).\n\n"
                    f"Чтобы интернет не отключился в самый подходящий момент, "
                    f"рекомендуем продлить её прямо сейчас! 🚀"
                )
                
                try:
                    await bot.send_message(user_id, text, reply_markup=builder_template.as_markup(), parse_mode="HTML")
                    print(f"✅ Обработан: {user_id}")
                except:
                    print(f"⚠️ Ошибка отправки: {user_id}")
                
                processed_count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Ошибка {user_id}: {e}")
                error_count += 1

        print(f"\nГотово! Обработано {processed_count}, ошибок {error_count}.")
    
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(process_inflated_subscriptions())
