# Yandex Cloud CDN + xHTTP + SneakerHub — пошаговая инструкция

Домен по умолчанию: **xdoublegroup.online**  
Origin VPS: **79.137.204.85** (Local-Node)

## ✅ Уже сделано на сервере

| Шаг гайда | Статус |
|-----------|--------|
| Selfsteal-сайт SneakerHub | ✅ `/var/www/sneakerhub/` |
| nginx конфиг origin | ✅ `/etc/nginx/sites-available/origin-cdn.conf` (не включён — ждёт сертификат) |
| XHTTP inbound в Remnawave | ✅ `XHTTP-SHOP` на `127.0.0.1:10085` |
| gRPC inbound | ✅ сохранён (`VLESS-gRPC` :8443) |
| Скрипт выпуска LE cert | ✅ `/root/SvoyVPN/scripts/setup-yandex-cdn-origin.sh` |

Проверка Xray:
```bash
ss -tlnp | grep 10085   # должен слушать 127.0.0.1:10085
```

---

## 🔴 Шаг 2. Cloudflare DNS (сделай ты)

Зайди в Cloudflare → зона **xdoublegroup.online** → DNS:

| Тип | Имя | Значение | Proxy |
|-----|-----|----------|-------|
| **A** | `origin` | `79.137.204.85` | **DNS only** (серое облако) |

⚠️ Оранжевое облако сломает Let's Encrypt и Yandex CDN.

После добавления записи — напиши мне или запусти на сервере:

```bash
/root/SvoyVPN/scripts/setup-yandex-cdn-origin.sh
```

Скрипт выпустит сертификат `origin.xdoublegroup.online` и включит nginx.

---

## 🔴 Шаг 8–10. Yandex Cloud CDN + Certificate Manager (сделай ты)

### 8.1 Создать CDN-ресурс

[console.yandex.cloud](https://console.yandex.cloud) → **Cloud CDN** → **Создать ресурс**

| Поле | Значение |
|------|----------|
| Доступ к контенту | ВКЛ |
| Запрос контента | Из одного источника |
| Тип источника | **Сервер** |
| Доменное имя источника | `origin.xdoublegroup.online` |
| Протокол | **HTTPS** |
| SNI вручную | ВКЛ → `origin.xdoublegroup.online` |
| Host заголовок | Своё → `origin.xdoublegroup.online` |
| Клиентский домен | `shop.xdoublegroup.online` |
| Тип сертификата | **Не использовать** (пока) |

**Кеширование — всё ВЫКЛ:**
- Кэширование в CDN — ВЫКЛ
- Кэширование в браузере — ВЫКЛ
- **Игнорировать cookies — ВЫКЛ** ← критично!
- Игнорировать query — ВЫКЛ
- gzip — ВЫКЛ
- Сегментация больших файлов — ВЫКЛ

**Методы:** только **GET, HEAD, OPTIONS**

После создания → **Обзор** → **Настройки DNS** → скопируй CNAME вида:
`bac6XXXXX.topology.gslb.yccdn.ru`

### 9. Certificate Manager

Yandex Cloud → **Certificate Manager** → **+ Добавить** → **От Let's Encrypt**

| Поле | Значение |
|------|----------|
| Имя | `shop-xdoublegroup-online` |
| Домены | `shop.xdoublegroup.online` |
| Проверка | **DNS CNAME** |

Yandex выдаст CNAME для `_acme-challenge.shop` — добавь в Cloudflare:

| Тип | Имя | Значение | Proxy |
|-----|-----|----------|-------|
| CNAME | `_acme-challenge.shop` | `fpqXXXX.cm.yandexcloud.net` | DNS only |

Жди статус **Issued** (5–15 мин, при ошибке жми «Повторить»).

### 9.3 CNAME для shop

| Тип | Имя | Значение | Proxy |
|-----|-----|----------|-------|
| CNAME | `shop` | `bac6XXXXX.topology.gslb.yccdn.ru` | DNS only |

### 10. Привязать cert к CDN

CDN → ресурс → **Редактировать** → тип сертификата: **Certificate Manager** → выбери `shop-xdoublegroup-online` → Сохранить.

Жди 5–20 мин, проверь:
```bash
curl -s "https://shop.xdoublegroup.online/" -o /dev/null -w "HTTP %{http_code}\n"
# HTTP 200 — магазин SneakerHub
```

---

## 🟡 Шаг 11. Host в Remnawave (сделаю после CDN)

Когда `shop.xdoublegroup.online` отвечает 200, создаём Host:

| Поле | Значение |
|------|----------|
| Примечание | `Russia v3 — SneakerHub` |
| Inbound | `XHTTP-SHOP` |
| Адрес | `shop.xdoublegroup.online` |
| Порт | `443` |
| SNI / Host | `shop.xdoublegroup.online` |
| Путь | `/api/cart/sync` |
| Security | TLS |
| ALPN | `h2,http/1.1` |

xHTTP extra (cookies + HEAD):
```json
{
  "path": "/api/cart/sync",
  "seqKey": "chunk",
  "seqPlacement": "cookie",
  "sessionKey": "visitor_id",
  "sessionPlacement": "cookie",
  "xPaddingKey": "_ts",
  "xPaddingHeader": "X-Cache-Status",
  "xPaddingMethod": "tokenish",
  "xPaddingPlacement": "queryInHeader",
  "xPaddingObfsMode": true,
  "xPaddingBytes": "100-1000",
  "uplinkHTTPMethod": "HEAD",
  "uplinkDataPlacement": "body",
  "uplinkChunkSize": 0
}
```

---

## 🧪 Финальный тест

```bash
# С сервера
curl -sk https://origin.xdoublegroup.online/ -w "origin: %{http_code}\n" -o /dev/null
curl -sk https://shop.xdoublegroup.online/ -w "shop CDN: %{http_code}\n" -o /dev/null
curl -sk -X HEAD https://shop.xdoublegroup.online/api/cart/sync -w "tunnel path: %{http_code}\n" -o /dev/null
```

В Happ: импорт подписки → сервер **Russia v3 — SneakerHub** → ifconfig.me показывает IP Yandex edge (`188.72.x.x`).

---

## Доступы чтобы я сделал Yandex/Cloudflare сам

1. **Cloudflare API Token** (Zone DNS Edit для xdoublegroup.online)
2. **Yandex Cloud** — сервисный аккаунт с ролями `cdn.editor` + `certificate-manager.editor` + JSON-ключ
3. Или просто логин/пароль в консоль (менее безопасно)

Положи ключи на сервер:
- `/root/.config/yandex-cloud/sa-key.json`
- `/root/.config/cloudflare/token`

---

## Важно

- **POST/PUT через Yandex CDN не работают** — поэтому `uplinkHTTPMethod: "HEAD"`
- Панель `www.xdoublegroup.online` на том же 443 — **не затронута** (отдельный `server_name`)
- Текущий gRPC bypass продолжает работать параллельно
