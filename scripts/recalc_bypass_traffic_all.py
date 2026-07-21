"""
Одноразовый пересчёт bypass-лимита (ГБ) для всех пользователей.

После фикса раздельных Remnawave-identity старые bypass_traffic_used_bytes
могли включать трафик с обычных (не-bypass) серверов. Скрипт:
  1) синхронизирует lifetime с панелей (traffic worker);
  2) ставит baseline = lifetime на всех bypass-ключах (дельта периода = 0);
  3) обнуляет users.bypass_traffic_used_bytes;
  4) пересчитывает агрегат (как воркер).

Пакеты bypass_bonus_gb не трогаем.
"""
from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv

load_dotenv("/root/SvoyVPN/.env")

from reset_bypass_meter import main as reset_bypass_meter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("recalc_bypass_traffic")


async def main() -> None:
    logger.warning(
        "This command now uses the dedicated-node meter migration; "
        "the old global UUID recalculation was unsafe."
    )
    await reset_bypass_meter()


if __name__ == "__main__":
    asyncio.run(main())
