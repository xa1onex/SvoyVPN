"""
Telegram custom emoji (RestrictedEmoji pack) с Unicode-fallback.

Сообщения: e("success") -> <tg-emoji> или ✅
Кнопки: emoji_button("Назад", "back", callback_data=...)
Happ / VPN-клиент: raw("success") -> только Unicode
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram.types import InlineKeyboardButton

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"
_IDS_FILE = _DATA_DIR / "custom_emoji_ids.json"

# Unicode fallback + optional override ID (RestrictedEmoji, t.me/addemoji/RestrictedEmoji)
_EMOJI: dict[str, tuple[str, str | None]] = {
    "success": ("✅", "5427009714745517609"),
    "error": ("❌", "5465665476971471368"),
    "warning": ("⚠️", "5467928559664242360"),  # ❗️
    "wait": ("⏳", "5451732530048802485"),
    "pause": ("⏸", "5451732530048802485"),
    "pause_vs": ("⏸️", "5451732530048802485"),
    "on": ("🟢", "5427009714745517609"),
    "off": ("🔴", "5465665476971471368"),
    "blocked": ("🚫", "5465665476971471368"),
    "stop": ("⛔", "5465665476971471368"),
    "back": ("◀️", "5469735272017043817"),  # 👈
    "forward": ("▶️", "5471978009449731768"),  # 👉
    "home": ("🏠", "5465226866321268133"),
    "main_menu": ("⏪", "5465226866321268133"),
    "bottom": ("🔙", "5469735272017043817"),
    "top": ("🔝", "5422354988103901774"),
    "point_right": ("👉", "5471978009449731768"),
    "point_down": ("👇", "5470177992950946662"),
    "vpn_connect": ("🔗", "5375129357373165375"),
    "subscription": ("🚀", "5445284980978621387"),
    "plus": ("💎", "5471952986970267163"),
    "limits": ("⏫", "5364105043907716258"),  # 🆙
    "gift": ("🎁", "5199749070830197566"),
    "help": ("🆘", "5467666648263564704"),  # ❓
    "devices": ("📱", "5407025283456835913"),
    "iphone": ("📱", "5407025283456835913"),
    "admin": ("🔐", "5472308992514464048"),
    "wave": ("👋", "5472055112702629499"),
    "free": ("🆓", "5364112491381006601"),
    "activate": ("⚡", "5431449001532594346"),
    "card": ("💳", "5375296873982604963"),  # 💰
    "star": ("⭐", "5435957248314579621"),
    "coin": ("🪙", "5379600444098093058"),
    "money": ("💰", "5375296873982604963"),
    "hot": ("🔥", "5420315771991497307"),
    "star_plus": ("🌟", "5458799228719472718"),
    "sad": ("😢", "5370881342659631698"),
    "party": ("🎉", "5436040291507247633"),
    "bypass": ("🔓", "5330115548900501467"),  # 🔑
    "signal": ("📶", "5431577498364158238"),
    "chart": ("📊", "5431577498364158238"),
    "globe": ("🌐", "5399898266265475100"),
    "refresh": ("🔄", "5264727218734524899"),
    "alert_double": ("‼️", "5467890025217661107"),
    "info": ("ℹ️", "5467666648263564704"),
    "info_link": ("🛈", "5467666648263564704"),
    "up": ("⬆️", "5364105043907716258"),
    "download": ("📥", "5433811242135331842"),
    "servers": ("🖥️", "5431376038628171216"),
    "desktop": ("🖥", "5431376038628171216"),
    "laptop": ("💻", "5431376038628171216"),
    "macos": ("🖥", "5190458330719461749"),
    "android": ("🤖", "5372981976804366741"),
    "linux": ("🐧", "5361541227604878624"),
    "windows": ("🪟", "5431376038628171216"),
    "power": ("⏻", "5431449001532594346"),
    "trash": ("🗑", "5465665476971471368"),
    "trash_vs": ("🗑️", "5465665476971471368"),
    "invite": ("📤", "5433614747381538714"),
    "copy": ("📑", "5377844313575150051"),
    "clipboard": ("📋", "5431577498364158238"),
    "history": ("📋", "5431577498364158238"),
    "user": ("👤", "5373012449597335010"),
    "users": ("👥", "5372926953978341366"),
    "handshake": ("🤝", "5357080225463149588"),
    "bulb": ("💡", "5472146462362048818"),
    "clock": ("⏰", "5413704112220949842"),
    "think": ("🤔", "5370724846936267183"),
    "envelope_msg": ("📨", "5406631276042002796"),
    "megaphone": ("📢", "5469903029144657419"),
    "support": ("🛟", "5465169893580086142"),
    "doc": ("📄", "5377844313575150051"),
    "pin": ("📌", "5188217332748527444"),
    "edit": ("✏️", "5334673106202010226"),
    "add": ("➕", "5226945370684140473"),
    "remove": ("➖", "5229113891081956317"),
    "calendar": ("📅", "5431897022456145283"),
    "gear": ("⚙️", "5330115548900501467"),
    "radio": ("🔘", "5188217332748527444"),
    "trend_up": ("📈", "5373001317042101552"),
    "trend_down": ("📉", "5361748661640372834"),
    "note": ("📝", "5334882760735598374"),
    "antenna": ("📡", "5406631276042002796"),
    "package": ("📦", "5433653135799228968"),
    "empty": ("📭", "5352896944496728039"),
    "camera": ("📸", "5375074927252621134"),
    "search": ("🔍", "5188217332748527444"),
    "test": ("🧪", "5411512278740640309"),
    "world": ("🌍", "5399898266265475100"),
    "wave_water": ("🌊", "5399898266265475100"),
    "plug": ("🔌", "5330115548900501467"),
    "key": ("🔑", "5330115548900501467"),
    "numbers": ("🔢", "5226470789682833538"),
    "keyboard": ("⌨️", "5472111548572900003"),
    "bell": ("🔔", "5242628160297641831"),
    "time": ("🕒", "5413704112220949842"),
    "see_no": ("🙈", "5467370583282950466"),
    "wrench": ("🔧", "5449428597922079323"),
    "skip": ("⏭️", "5471978009449731768"),
    "email": ("📧", "5406631276042002796"),
    "timer": ("⏱", "5413704112220949842"),
    "smile": ("🙂", "5371073319107827779"),
    "photo": ("🖼", "5375074927252621134"),
    "photo_vs": ("🖼️", "5375074927252621134"),
    "video": ("🎥", "5375309569905938163"),
    "gif": ("🎬", "5375464961822695044"),
    "attach": ("📎", "5377844313575150051"),
    "contact": ("📇", "5373012449597335010"),
    "chat": ("💬", "5465300082628763143"),
    "tag": ("🏷", "5188217332748527444"),
    "tag_vs": ("🏷️", "5188217332748527444"),
    "compass": ("🧭", "5433825729060018456"),
    "eu": ("🇪🇺", "5228784522924930237"),
    "sparkle": ("💫", "5469741319330996757"),
    "pl": ("🇵🇱", "5291847690940852675"),
    "start": ("▶️", "5471978009449731768"),
}


@dataclass(frozen=True)
class EmojiSpec:
    key: str
    fallback: str
    custom_id: str | None


_overrides: dict[str, str] = {}


def _load_overrides() -> None:
    global _overrides
    merged: dict[str, str] = {}
    if _IDS_FILE.exists():
        try:
            raw = json.loads(_IDS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged.update({k: str(v) for k, v in raw.items() if v})
        except Exception as exc:
            logger.warning("custom_emoji_ids.json: %s", exc)
    _overrides = merged


def reload_emoji_ids() -> None:
    _load_overrides()


_load_overrides()


def _spec(key: str) -> EmojiSpec:
    if key not in _EMOJI:
        raise KeyError(f"Unknown emoji key: {key}")
    fallback, default_id = _EMOJI[key]
    custom_id = _overrides.get(key) or default_id
    return EmojiSpec(key=key, fallback=fallback, custom_id=custom_id)


def raw(key: str) -> str:
    """Unicode fallback — для Happ, логов VPN-клиента и т.п."""
    return _spec(key).fallback


def e(key: str) -> str:
    """HTML-фрагмент для текста/caption в Telegram."""
    spec = _spec(key)
    if spec.custom_id:
        return f'<tg-emoji emoji-id="{spec.custom_id}">{spec.fallback}</tg-emoji>'
    return spec.fallback


def lbl(key: str, text: str) -> str:
    """Эмодзи + пробел + текст (для подписей кнопок/лейблов в сообщениях)."""
    return f"{e(key)} {text}"


def toast(key: str, text: str) -> str:
    """Plain text + Unicode emoji — для callback.answer (без HTML)."""
    return f"{raw(key)} {text}"


def icon_id(key: str) -> str | None:
    return _spec(key).custom_id


def emoji_button(text: str, emoji_key: str, **kwargs: Any) -> InlineKeyboardButton:
    """Inline-кнопка с custom emoji-иконкой (Bot API 9.4+)."""
    btn_kwargs = dict(kwargs)
    cid = icon_id(emoji_key)
    if cid:
        btn_kwargs["icon_custom_emoji_id"] = cid
    return InlineKeyboardButton(text=text, **btn_kwargs)


class _EmojiAccessor:
    """f\"{E.success} текст\" — без вложенных кавычек (Python 3.10+)."""

    def __getattr__(self, key: str) -> str:
        return e(key)


E = _EmojiAccessor()


def btn(text: str, emoji_key: str, **kwargs: Any) -> InlineKeyboardButton:
    """Кнопка с Unicode-эмодzi в подписи (видно на всех клиентах)."""
    return btn_labeled(text, emoji_key, **kwargs)


def btn_labeled(text: str, emoji_key: str, **kwargs: Any) -> InlineKeyboardButton:
    """Кнопка с Unicode-эмодзи в тексте (видно всегда, без icon_custom_emoji_id)."""
    return InlineKeyboardButton(text=f"{raw(emoji_key)} {text}", **kwargs)


def copy_btn(text: str, emoji_key: str, *, copy_text: str, **kwargs: Any) -> InlineKeyboardButton:
    """Кнопка «скопировать текст» с custom emoji-иконкой."""
    from aiogram.types import CopyTextButton

    btn_kwargs = dict(kwargs)
    btn_kwargs["copy_text"] = CopyTextButton(text=copy_text)
    cid = icon_id(emoji_key)
    if cid:
        btn_kwargs["icon_custom_emoji_id"] = cid
    return InlineKeyboardButton(text=text, **btn_kwargs)
