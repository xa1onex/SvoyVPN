#!/usr/bin/env python3
"""
Скрипт для исправления VLESS-ссылок, содержащих 127.0.0.1.
Заменяет 127.0.0.1 на публичный IP сервера из таблицы servers.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from bot.database import get_connection

async def main():
    async with get_connection() as conn:
        # Получаем все ключи с 127.0.0.1 или localhost в ссылке
        keys = await conn.fetch('''
            SELECT k.id, k.vless_link, s.ip, s.name
            FROM vpn_keys k
            JOIN servers s ON k.server_id = s.id
            WHERE k.vless_link LIKE '%127.0.0.1%' OR k.vless_link LIKE '%localhost%'
        ''')
        
        if not keys:
            print("❌ Ключей с локальными IP не найдено.")
            return
            
        print(f"Найдено ключей для исправления: {len(keys)}")
        
        updated = 0
        for key in keys:
            key_id = key['id']
            old_link = key['vless_link']
            public_ip = key['ip']
            
            if not public_ip:
                print(f"⚠️ Пропуск ключа {key_id}: у сервера '{key['name']}' не указан публичный IP.")
                continue
                
            new_link = old_link.replace("127.0.0.1", public_ip).replace("localhost", public_ip)
            
            await conn.execute(
                "UPDATE vpn_keys SET vless_link = $1 WHERE id = $2",
                new_link, key_id
            )
            updated += 1
            print(f"✅ Исправлен ключ {key_id}: 127.0.0.1 → {public_ip}")
            
        print(f"\nИтог: исправлено {updated} ключей.")

if __name__ == "__main__":
    asyncio.run(main())
