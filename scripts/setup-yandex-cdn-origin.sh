#!/usr/bin/env bash
# Подготовка origin для Yandex CDN + xHTTP (гайд «Yandex Cloud CDN + xHTTP + selfsteal»)
# Запускать на VPS-ноде (79.137.204.85). DNS и Yandex Cloud — отдельно (см. docs/yandex-cdn-setup.md).

set -euo pipefail

DOMAIN="${DOMAIN:-xdoublegroup.online}"
ORIGIN_HOST="origin.${DOMAIN}"
SHOP_HOST="shop.${DOMAIN}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@${DOMAIN}}"

echo "==> Origin host: ${ORIGIN_HOST}"
echo "==> Shop host (CDN): ${SHOP_HOST}"

if ! dig +short "${ORIGIN_HOST}" A | grep -q .; then
  echo "ERROR: DNS A для ${ORIGIN_HOST} не найден."
  echo "Добавь в Cloudflare: A origin → IP VPS, Proxy = DNS only (серое облако)"
  exit 1
fi

echo "==> Останавливаем nginx для standalone certbot..."
systemctl stop nginx

if [[ ! -f "/etc/letsencrypt/live/${ORIGIN_HOST}/fullchain.pem" ]]; then
  certbot certonly --standalone -d "${ORIGIN_HOST}" \
    --non-interactive --agree-tos \
    -m "${ADMIN_EMAIL}" --no-eff-email
fi

echo "==> Включаем nginx origin-cdn..."
ln -sf /etc/nginx/sites-available/origin-cdn.conf /etc/nginx/sites-enabled/origin-cdn.conf
nginx -t
systemctl start nginx
systemctl enable nginx

echo "==> Проверка магазина..."
curl -sk "https://${ORIGIN_HOST}/" -o /dev/null -w "HTTP %{http_code} | size %{size_download}\n"

echo "==> Готово на origin. Дальше: Yandex Cloud CDN + Certificate Manager + CNAME shop → Yandex edge"
echo "    См. /root/SvoyVPN/docs/yandex-cdn-setup.md"
