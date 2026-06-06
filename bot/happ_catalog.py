"""
Человекочитаемые подписи для Happ (без протокола/transport в UI).

Реальные VLESS/Xray остаются внутри зашифрованного bundle; в списке
часть клиентов показывает ``remarks`` + ``meta.serverDescription``.
Нужны ``providerid`` (query и/или заголовок ответа) и при возможности —
``hide-settings`` в том же виде (_см._ ``happ_subscription_query_suffix``).

На **iOS** вторую строку чаще подставляет ``meta.serverDescription``.
На **macOS** многие сборки Happ по-прежнему рисуют «VLESS | JSON» и не
подхватывают ``meta`` — это известное различие клиента, а не отсутствие
полей в JSON. Заголовки ответа при импорте через ``happ://add/`` на Mac
иногда не применяются; поэтому ``hide-settings`` дублируется в query URL.

Параметр ``hide-settings`` скрывает экран настроек узла; не меняет факт
отображения технической второй строки там, где клиент игнорирует ``meta``.

Дополнительно дублируем ``hide-settings`` и ``providerid`` во фрагменте
limited-link URL (``happ_subscription_fragment_device_params``).

Текстовые «уведомления» в plain /sub (не JSON): см. ``happ_text_notice`` —
фрагмент ``#title?serverDescription=…`` в vless://.

Импорт через ``happ://crypt5/…`` (см. ``encrypt_profile``, ``/sub``).
"""

from __future__ import annotations

from .traffic import is_free_header_server, is_navigation_header_server


def _norm(s: str) -> str:
    return (s or "").lower()


def presentation_for_server(
    *,
    remark: str,
    server_name: str,
    is_bypass: bool,
    is_tg_relay: bool = False,
) -> tuple[str, str]:
    """
    (remarks — как в панели/vless; второй элемент — ``meta.serverDescription``,
    до 30 символов по лимиту Happ, строки короткие чтобы не обрезались в UI.)

    Правило: есть 🆓 в названии (или ``is_bypass`` с панели) → подпись «обход»;
    иначе всегда «быстрый» без привязки к стране.
    """
    raw = (remark or server_name or "Сервер").strip()
    blob = _norm(raw) + _norm(server_name or "")
    sn = server_name or ""

    if is_tg_relay or is_tg_relay_server_label(raw) or is_tg_relay_server_label(sn):
        return tg_relay_presentation()

    if is_free_header_server(raw) or is_free_header_server(sn):
        return (raw, "Информация")

    if is_navigation_header_server(raw) or is_navigation_header_server(sn):
        return (raw, "Информация")

    if is_bypass or "🆓" in raw or "🆓" in sn:
        return (raw, "Обход глушилок")

    return (raw, "Быстрый сервер")


def autoselect_presentation() -> tuple[str, str]:
    """Автовыбор в списке Happ + короткий подзаголовок (без стран)."""
    return (
        "🇪🇺 💫 Автовыбор",
        "Быстрый · авто",
    )


def tg_relay_presentation() -> tuple[str, str]:
    """Узел «ТГ безлимит» — всегда последний в подписке Happ."""
    return ("‼️ ТГ БЕЗЛИМИТ ‼️", "ТГ всегда работает")


def is_tg_relay_server_label(label: object) -> bool:
    s = str(label or "").lower()
    return "тг" in s and "безлимит" in s
