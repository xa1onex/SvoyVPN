"""
Человекочитаемые подписи для Happ (без протокола/transport в UI).

Реальные VLESS/Xray остаются внутри зашифрованного bundle; в списке
пользователь видит в основном remarks + meta.serverDescription.

Импорт «как у конкурентов» без сырого JSON в браузере: deeplink
happ://crypt5/… (см. webhook_server.handle_app_connect). Полностью скрыть
настройки внутри Happ для обычного JSON bundle нельзя — это ограничение клиента.
Mihomo/Clash proxy-groups в одной Happ-подписке недоступны без поддержки формата.
"""

from __future__ import annotations


def _norm(s: str) -> str:
    return (s or "").lower()


def presentation_for_server(
    *,
    remark: str,
    server_name: str,
    is_bypass: bool,
) -> tuple[str, str]:
    """
    (remarks — заголовок строки в Happ; второй элемент — текст для
    ``meta.serverDescription`` / подзаголовок; в генераторе обрезается до 30 символов.
    Без зарегистрированного Happ Provider ID клиент часто не показывает meta —
    тогда описание дублируется в ``remarks`` (см. profile_generator._happ_row_remarks).
    """
    raw = (remark or server_name or "Сервер").strip()
    blob = _norm(raw) + _norm(server_name or "")

    if is_bypass or "обход" in blob or "бел" in blob or "🆓" in raw:
        desc = "Используй, когда не глушат связь; обход ограничений"
        return (raw if raw else "🆓 Обход", desc)

    if "быстр" in blob or "🚀" in raw:
        desc = "Минимальный ping; оптимизировано под скорость"
        return (raw, desc)

    if "герман" in blob or "germany" in blob:
        desc = "Высокая скорость; стабильное соединение"
        return (raw, desc)

    if "польш" in blob or "poland" in blob:
        desc = "Оптимизировано под мобильные операторы"
        return (raw, desc)

    if "нидерлан" in blob or "netherland" in blob or "🇳🇱" in raw:
        desc = "Стабильное соединение; хорош для стриминга"
        return (raw, desc)

    if "росси" in blob or "russia" in blob or "🇷🇺" in raw:
        desc = "Региональный узел; низкая задержка в РФ"
        return (raw, desc)

    if "game" in blob or "игр" in blob:
        desc = "Для игр; низкий ping"
        return (raw, desc)

    if "stream" in blob or "youtube" in blob or "стрим" in blob:
        desc = "Для YouTube и стриминга"
        return (raw, desc)

    desc = "Универсальный узел; стабильное подключение"
    return (raw, desc)


def autoselect_presentation() -> tuple[str, str]:
    """Как раньше в UI: название автовыбора + короткое описание."""
    return (
        "🇪🇺 💫 Автовыбор",
        "Автовыбор стран",
    )
