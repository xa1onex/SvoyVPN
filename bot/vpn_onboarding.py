"""
Тексты и кнопки инструкции Happ по платформам (без выбора приложения).
"""
from __future__ import annotations

import html
import os
from pathlib import Path

from aiogram.types import FSInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

_IMAGES_DIR = Path(__file__).resolve().parent / "images"

HAPP_URLS = {
    "android_play": "https://play.google.com/store/apps/details?id=com.happproxy",
    "android_apk": "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk",
    "ios_main": "https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973",
    "ios_global": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
    "windows_setup": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe",
}

_DEVICE_PHOTO = {
    "android": _IMAGES_DIR / "android.png",
    "apple": _IMAGES_DIR / "ios.png",
    "mac": _IMAGES_DIR / "ios.png",
}

_BLOCK_VERSION = (
    "<blockquote>Перед активацией подписки убедись, что у тебя установлена "
    "последняя версия приложения, иначе может возникнуть ошибка.</blockquote>"
)


def _subscription_base_url(config=None) -> str:
    if config and getattr(config, "subscription_base_url", None):
        return str(config.subscription_base_url).rstrip("/")
    return (os.getenv("SUBSCRIPTION_BASE_URL") or "https://xdoublegroup.online").rstrip("/")


def activation_connect_url(device: str, token: str, config=None) -> str:
    """Страница активации (редирект в Happ)."""
    base = _subscription_base_url(config)
    device_path = "apple" if device == "mac" else device
    return f"{base}/{device_path}/happ/{token}"


def device_instruction_photo(device: str) -> FSInputFile | None:
    path = _DEVICE_PHOTO.get(device)
    if path and path.is_file():
        return FSInputFile(path)
    return None


def _link(url: str, label: str) -> str:
    return f'<a href="{url}"><b>{html.escape(label)}</b></a>'


def _install_links_android() -> str:
    return (
        f'{_link(HAPP_URLS["android_play"], "Из Google Play")}'
        f' | {_link(HAPP_URLS["android_apk"], "Ссылка на APK")}'
    )


def _install_links_ios() -> str:
    return (
        f'{_link(HAPP_URLS["ios_main"], "Основная версия")}'
        f' | {_link(HAPP_URLS["ios_global"], "Глобальная версия")}'
    )


def _blockquote_manual(sub_url: str) -> str:
    esc = html.escape(sub_url, quote=False)
    return (
        "<blockquote>Если после активации подписки страны не появились, скопируйте эту ссылку "
        'и в happ через "+" добавьте конфиг → ссылка без редиректа в '
        f"<code>{esc}</code></blockquote>"
    )


def _add_download_buttons(builder: InlineKeyboardBuilder, device: str) -> None:
    if device == "android":
        builder.row(
            InlineKeyboardButton(text="📥 Google Play", url=HAPP_URLS["android_play"]),
            InlineKeyboardButton(text="📥 APK", url=HAPP_URLS["android_apk"]),
        )
    elif device in ("apple", "mac"):
        builder.row(
            InlineKeyboardButton(text="📥 Основная версия", url=HAPP_URLS["ios_main"]),
            InlineKeyboardButton(text="📥 Глобальная версия", url=HAPP_URLS["ios_global"]),
        )
    elif device == "windows":
        builder.row(
            InlineKeyboardButton(
                text="📥 Скачать приложение",
                url=HAPP_URLS["windows_setup"],
            ),
        )


async def build_happ_instruction_async(
    device: str,
    user_id: int,
    *,
    token: str,
    config=None,
) -> tuple[str, InlineKeyboardBuilder]:
    from .subscriptions import get_user_subscription_url

    sub_url = await get_user_subscription_url(user_id, config)
    connect_url = activation_connect_url(device, token, config)
    text = _instruction_text(device, sub_url, connect_url)
    builder = InlineKeyboardBuilder()
    _add_download_buttons(builder, device)
    builder.row(
        InlineKeyboardButton(text="⚡ Активировать подписку", url=connect_url)
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="ob_back_devices"))
    return text, builder


def _instruction_text(device: str, sub_url: str, connect_url: str) -> str:
    manual = _blockquote_manual(sub_url)
    activate = _link(connect_url, "Активировать подписку")

    if device == "android":
        return (
            "<b>Настройка приложения Happ для Android</b>\n\n"
            "1. Установи приложение ↓\n"
            f"{_install_links_android()}\n"
            f"{_BLOCK_VERSION}\n\n"
            "2. Перейди по ссылке для активации подписки → "
            f"{activate}\n"
            f"{manual}\n\n"
            "3. Для подключения выбери страну, нажав на неё, "
            "а затем нажми кнопку «ВКЛ»."
        )

    if device == "windows":
        return (
            "<b>Настройка приложения Happ для Windows</b>\n\n"
            "1. Установи приложение → "
            f'{_link(HAPP_URLS["windows_setup"], "Начать установку")}\n'
            f"{_BLOCK_VERSION}\n\n"
            "2. Перейди по ссылке для активации подписки → "
            f"{activate}\n"
            f"{manual}\n\n"
            "3. Для подключения выбери страну, нажав на неё, "
            "а затем нажми кнопку «ВКЛ»."
        )

    if device == "mac":
        return (
            "<b>Настройка приложения Happ для MacOS</b>\n\n"
            "1. Установи приложение ↓\n"
            f"{_install_links_ios()}\n"
            f"{_BLOCK_VERSION}\n\n"
            "2. Перейди по ссылке для активации подписки → "
            f"{activate}\n"
            f"{manual}\n\n"
            "3. Для подключения выбери страну, нажав на неё, "
            "а затем нажми кнопку ⏻ включения."
        )

    return (
        "<b>Настройка приложения Happ для iPhone, iPad</b>\n\n"
        "Если в магазине App Store пишет «Приложение недоступно», попробуй найти его "
        "через поиск, введя «Happ - Proxy».\n\n"
        "1. Установи приложение ↓\n"
        f"{_install_links_ios()}\n"
        f"{_BLOCK_VERSION}\n\n"
        "2. Перейди по ссылке для активации подписки → "
        f"{activate}\n"
        f"{manual}\n\n"
        "3. Для подключения выбери страну, нажав на неё, "
        "а затем нажми кнопку ⏻ включения."
    )
