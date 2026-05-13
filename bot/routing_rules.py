"""
Правила маршрутизации для bypass российских сервисов.

Трафик к этим доменам/IP идёт напрямую (direct), остальной — через VPN.
"""

from __future__ import annotations

RU_BYPASS_DOMAINS: list[str] = [
    # --- Банки ---
    "domain:sberbank.ru",
    "domain:online.sberbank.ru",
    "domain:sbol.ru",
    "domain:tinkoff.ru",
    "domain:tbank.ru",
    "domain:alfabank.ru",
    "domain:alfadirect.ru",
    "domain:vtb.ru",
    "domain:open.ru",
    "domain:raiffeisen.ru",
    "domain:rshb.ru",
    "domain:gazprombank.ru",
    "domain:sovcombank.ru",
    "domain:psbank.ru",
    "domain:mkb.ru",
    "domain:rosbank.ru",
    "domain:uralsib.ru",
    "domain:unicreditbank.ru",
    "domain:bnkv.ru",
    "domain:pochtabank.ru",
    "domain:homecredit.ru",
    "domain:roscap.ru",
    "domain:otp.ru",
    "domain:rencredit.ru",
    "domain:mtsbank.ru",
    "domain:citibank.ru",

    # --- Госуслуги / Гос. сервисы ---
    "domain:gosuslugi.ru",
    "domain:mos.ru",
    "domain:nalog.ru",
    "domain:pfr.gov.ru",
    "domain:fss.ru",
    "domain:esia.gosuslugi.ru",
    "domain:lk.gosuslugi.ru",
    "domain:zakupki.gov.ru",
    "domain:cbr.ru",
    "domain:gov.ru",

    # --- VK экосистема ---
    "domain:vk.com",
    "domain:vk.me",
    "domain:vk.ru",
    "domain:vkontakte.ru",
    "domain:vkusvill.ru",
    "domain:userapi.com",
    "domain:vk-cdn.net",
    "domain:vkuser.net",
    "domain:vkusershare.com",
    "domain:vk.cc",
    "domain:vkontakte.ru",
    "domain:dzen.ru",
    "domain:zen.yandex.ru",
    "domain:mail.ru",
    "domain:my.mail.ru",
    "domain:ok.ru",
    "domain:odnoklassniki.ru",

    # --- Yandex ---
    "domain:yandex.ru",
    "domain:yandex.net",
    "domain:yandex.com",
    "domain:ya.ru",
    "domain:yastatic.net",
    "domain:yandex.st",
    "domain:yandexcloud.net",
    "domain:yandex-team.ru",
    "domain:music.yandex.ru",
    "domain:disk.yandex.ru",
    "domain:taxi.yandex.ru",
    "domain:eda.yandex.ru",
    "domain:market.yandex.ru",
    "domain:lavka.yandex.ru",
    "domain:afisha.yandex.ru",
    "domain:kinopoisk.ru",
    "domain:plus.yandex.ru",
    "domain:alice.yandex.ru",
    "domain:passport.yandex.ru",

    # --- Маркетплейсы ---
    "domain:wildberries.ru",
    "domain:wb.ru",
    "domain:wbstatic.net",
    "domain:wbbasket.ru",
    "domain:wbcontent.net",
    "domain:ozon.ru",
    "domain:ozon.st",
    "domain:ozoncloud.ru",
    "domain:ozoncdn.ru",
    "domain:avito.ru",
    "domain:avito.st",
    "domain:mds.yandex.net",
    "domain:beru.ru",

    # --- Доставка / Еда ---
    "domain:delivery-club.ru",
    "domain:samokat.ru",
    "domain:sbermarket.ru",
    "domain:perekrestok.ru",
    "domain:lenta.com",
    "domain:magnit.ru",
    "domain:pyaterochka.ru",
    "domain:5ka.ru",

    # --- Телеком ---
    "domain:mts.ru",
    "domain:megafon.ru",
    "domain:beeline.ru",
    "domain:tele2.ru",
    "domain:t2.ru",
    "domain:yota.ru",
    "domain:rt.ru",
    "domain:rostelecom.ru",

    # --- Такси / Транспорт ---
    "domain:taxi.yandex.ru",
    "domain:city-mobil.ru",
    "domain:gett.com",
    "domain:rzd.ru",
    "domain:tutu.ru",
    "domain:aviasales.ru",
    "domain:kupibilet.ru",
    "domain:aeroflot.ru",
    "domain:s7.ru",
    "domain:pobeda.aero",

    # --- Медиа ---
    "domain:ivi.ru",
    "domain:okko.tv",
    "domain:more.tv",
    "domain:premier.one",
    "domain:start.ru",
    "domain:wink.ru",
    "domain:rutube.ru",
    "domain:tnt-online.ru",

    # --- Карты ---
    "domain:2gis.ru",
    "domain:2gis.com",

    # --- Другие популярные ---
    "domain:habr.com",
    "domain:pikabu.ru",
    "domain:sports.ru",
    "domain:championat.com",
    "domain:rbc.ru",
    "domain:ria.ru",
    "domain:tass.ru",
    "domain:lenta.ru",
    "domain:gazeta.ru",
    "domain:kommersant.ru",
    "domain:vedomosti.ru",
    "domain:fontanka.ru",
    "domain:1c.ru",
    "domain:consultant.ru",
    "domain:garant.ru",
    "domain:hh.ru",
    "domain:superjob.ru",
    "domain:cian.ru",
    "domain:domclick.ru",
    "domain:youla.ru",
]

RU_BYPASS_GEOIP: list[str] = [
    "geoip:ru",
]

RU_BYPASS_IP_CIDR: list[str] = [
    "geoip:private",
]


def get_direct_domain_rules() -> list[str]:
    """Возвращает список доменов для direct routing (без префикса 'domain:')."""
    return [d.removeprefix("domain:") for d in RU_BYPASS_DOMAINS]


def get_singbox_bypass_domains() -> list[str]:
    """Возвращает domain_suffix список для sing-box route rules."""
    return [d.removeprefix("domain:") for d in RU_BYPASS_DOMAINS]


def get_xray_routing_rules() -> list[dict]:
    """Xray-формат routing rules для direct bypass."""
    domains = [d for d in RU_BYPASS_DOMAINS]
    return [
        {
            "type": "field",
            "outboundTag": "direct",
            "domain": domains,
        },
        {
            "type": "field",
            "outboundTag": "direct",
            "ip": ["geoip:ru", "geoip:private"],
        },
        {
            "type": "field",
            "outboundTag": "direct",
            "protocol": ["bittorrent"],
        },
    ]
