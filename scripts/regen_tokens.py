#!/usr/bin/env python3
"""
Скрипт перегенерации subscription_token для всех пользователей.

Заменяет длинные токены (~43 символа) на короткие (~19 символов).
Запускать ОДИН РАЗ на сервере:

    cd /root/SvoyVPN
    source venv/bin/activate
    python3 scripts/regen_tokens.py

ВАЖНО: после запуска все старые ссылки перестанут работать.
Пользователи получат новые ссылки при следующем обращении к боту.
"""

import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def main():
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        pool = await asyncpg.create_pool(dsn=db_url, min_size=1, max_size=5)
    else:
        pool = await asyncpg.create_pool(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            database=os.getenv("DB_NAME", "vpn_db"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
            min_size=1,
            max_size=5,
        )

    async with pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT user_id, subscription_token FROM users ORDER BY user_id"
        )
        total = len(users)
        print(f"Найдено пользователей: {total}")

        updated = 0
        skipped = 0
        errors = 0

        for user in users:
            user_id = user["user_id"]
            old_token = user["subscription_token"]

            # Пропускаем если токена нет (будет сгенерирован при первом обращении)
            if not old_token:
                skipped += 1
                continue

            # Пропускаем уже короткие токены (≤20 символов)
            if len(old_token) <= 20:
                skipped += 1
                continue

            # Пытаемся сократить текущий токен (взять первые 19 символов)
            # Это сохранит совместимость старых ссылок с новым поиском по префиксу
            new_token = old_token[:19]
            
            # Если вдруг сокращенный токен совпал с чьим-то (маловероятно),
            # генерируем полностью новый
            try:
                result = await conn.execute(
                    "UPDATE users SET subscription_token = $1 WHERE user_id = $2",
                    new_token,
                    user_id,
                )
                updated += 1
                print(f"  ✅ {user_id}: {old_token[:12]}… → {new_token}")
            except asyncpg.UniqueViolationError:
                # Генерируем новый короткий токен, если коллизия
                for attempt in range(10):
                    new_token = secrets.token_urlsafe(14)
                    try:
                        await conn.execute(
                            "UPDATE users SET subscription_token = $1 WHERE user_id = $2",
                            new_token,
                            user_id,
                        )
                        updated += 1
                        print(f"  🆕 {user_id}: коллизия, новый токен → {new_token}")
                        break
                    except asyncpg.UniqueViolationError:
                        if attempt == 9:
                            print(f"  ❌ Не удалось обновить {user_id}")
                            errors += 1

    await pool.close()

    print()
    print("=" * 50)
    print(f"Обновлено:  {updated}")
    print(f"Пропущено:  {skipped}")
    print(f"Ошибки:     {errors}")
    print("=" * 50)
    print()
    print("✅ Готово! Перезапустите бота: systemctl restart svoyvpn")


if __name__ == "__main__":
    asyncio.run(main())
