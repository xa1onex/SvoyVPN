# Исправление: stable passing 5/5 failed (CNAME + Cloudflare)

## Причина

CNAME `_acme-challenge.shop` → `fpqXXX.cm.yandexcloud.net` через Cloudflare **нестабилен** для Google DNS (8.8.8.8): периодически **NXDOMAIN**. Yandex делает 5 проверок подряд — одной ошибки достаточно.

## Решение: перейти на TXT (надёжнее)

### Шаг 1. Удали старый сертификат в Yandex CM

Сертификат после 5/5 обычно **Invalid** — удали `shop-xdoublegroup-online`.

### Шаг 2. Создай новый сертификат

Certificate Manager → **+ Добавить** → **От Let's Encrypt**

| Поле | Значение |
|------|----------|
| Имя | `shop-xdoublegroup-online` |
| Домены | `shop.xdoublegroup.online` |
| Тип проверки | **DNS TXT** (не CNAME!) |

### Шаг 3. Cloudflare — удали CNAME, добавь TXT

**Удали** запись `_acme-challenge.shop` (CNAME), если есть.

**Добавь** (значение скопируй из Yandex CM → вкладка **TXT-запись**):

| Type | Name | Content | Proxy |
|------|------|---------|-------|
| TXT | `_acme-challenge.shop` | `<токен из Yandex>` | DNS only |

⚠️ Только TXT, **без CNAME** на том же имени.

### Шаг 4. Проверка на сервере

```bash
/root/SvoyVPN/scripts/verify-yandex-acme-dns.sh --txt shop.xdoublegroup.online 'ВАШ_ТОКЕН_ИЗ_YANDEX'
```

Все три резолвера должны быть ✅.

### Шаг 5. Подожди 5 мин → в Yandex CM «Повторить»

Статус должен стать **Issued**.

---

## После Issued

1. CDN → привязать cert к ресурсу
2. Cloudflare: CNAME `shop` → `bac6XXXX.topology.gslb.yccdn.ru`
3. Написать в чат — настроим Host в Remnawave

---

## Минус TXT

Продление ~раз в 60 дней — нужно обновлять TXT (CNAME делегирует это Yandex автоматически). Для стабильного первого выпуска TXT надёжнее.
