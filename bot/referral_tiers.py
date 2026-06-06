"""
Лестница реферальных комиссий по суммарному обороту приглашённых (всё время).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

# (порог оборота в копейках, процент комиссии)
REFERRAL_TIERS: list[tuple[int, int]] = [
    (0, 20),
    (200_000, 25),       # 2 000 ₽
    (1_000_000, 30),     # 10 000 ₽
    (3_000_000, 35),     # 30 000 ₽
    (10_000_000, 40),    # 100 000 ₽
]

MONTH_NAMES_RU = (
    "", "январе", "феврале", "марте", "апреле", "мае", "июне",
    "июле", "августе", "сентябре", "октябре", "ноябре", "декабре",
)


def current_year_month_msk() -> tuple[int, int]:
    now = datetime.now(MSK)
    return now.year, now.month


def month_bounds_msk(year: int, month: int) -> tuple[datetime, datetime]:
    """[start, end) календарного месяца MSK как naive UTC для TIMESTAMP WITHOUT TIME ZONE."""
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=MSK)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=MSK)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=MSK)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


# naive UTC в БД → календарный месяц в Europe/Moscow
_MSK_YM_SQL = """
    EXTRACT(YEAR FROM ({col} AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::int = ${year}
    AND EXTRACT(MONTH FROM ({col} AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::int = ${month}
"""


def msk_year_month_sql(column: str, year_param: int, month_param: int) -> str:
    return _MSK_YM_SQL.format(col=column, year=year_param, month=month_param)


def month_label_ru(year: int, month: int) -> str:
    return f"{MONTH_NAMES_RU[month]} {year}"


def rate_for_volume_cents(volume_cents: int) -> int:
    rate = REFERRAL_TIERS[0][1]
    for threshold, pct in REFERRAL_TIERS:
        if volume_cents >= threshold:
            rate = pct
    return rate


def next_tier_info(volume_cents: int) -> tuple[int | None, int | None]:
    """(следующий %, копейки до порога) или (None, None) если максимум."""
    for threshold, pct in REFERRAL_TIERS:
        if volume_cents < threshold:
            return pct, threshold - volume_cents
    return None, None


def format_threshold_rub(threshold_cents: int) -> str:
    if threshold_cents <= 0:
        return "0 ₽"
    rub = threshold_cents // 100
    return f"{rub:,}".replace(",", " ") + " ₽"


def format_tiers_blockquote(
    current_rate: int, *, title: str | None = "Уровни"
) -> str:
    """Шкала уровней в <blockquote> для Telegram HTML."""
    lines: list[str] = []
    for threshold, pct in REFERRAL_TIERS:
        mark = " ← вы" if pct == current_rate else ""
        lines.append(f"{pct}% — от {format_threshold_rub(threshold)}{mark}")
    body = f"<blockquote>{chr(10).join(lines)}</blockquote>"
    if title:
        return f"<b>{title}</b>\n{body}"
    return body


def tier_notification_type(rate: int) -> str:
    return f"referral_tier_{rate}"
