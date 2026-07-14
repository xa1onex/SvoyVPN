#!/usr/bin/env bash
# Проверка DNS для Yandex Certificate Manager
# CNAME: ./verify-yandex-acme-dns.sh shop.xdoublegroup.online fpqXXX.cm.yandexcloud.net
# TXT:   ./verify-yandex-acme-dns.sh --txt shop.xdoublegroup.online 'TOKEN_FROM_YANDEX'
set -euo pipefail

MODE="cname"
if [[ "${1:-}" == "--txt" ]]; then
  MODE="txt"
  shift
fi

DOMAIN="${1:-shop.xdoublegroup.online}"
CHALLENGE="_acme-challenge.${DOMAIN}"

echo "=== Режим: ${MODE} | ${CHALLENGE} ==="

if [[ "${MODE}" == "txt" ]]; then
  EXPECTED_TXT="${2:-}"
  if [[ -z "${EXPECTED_TXT}" ]]; then
    echo "❌ Укажи TXT-токен из Yandex CM (вкладка TXT-запись)"
    echo "   ./verify-yandex-acme-dns.sh --txt shop.xdoublegroup.online 'YOUR_TOKEN'"
    exit 1
  fi

  CNAME=$(dig +short "${CHALLENGE}" CNAME | head -1)
  if [[ -n "${CNAME}" ]]; then
    echo "❌ Найден CNAME на ${CHALLENGE} — удали CNAME, должен быть ТОЛЬКО TXT"
    exit 1
  fi

  GOT=$(dig +short "${CHALLENGE}" TXT | tr -d '"' | head -1)
  if [[ "${GOT}" != "${EXPECTED_TXT}" ]]; then
    echo "❌ TXT не совпадает"
    echo "   Ожидается: ${EXPECTED_TXT}"
    echo "   В DNS:     ${GOT:-пусто}"
    exit 1
  fi
  echo "✅ TXT на месте: ${GOT:0:24}..."

  FAIL=0
  for r in 1.1.1.1 8.8.8.8 77.88.8.8; do
    st=$(dig @"${r}" "${CHALLENGE}" TXT +comments 2>/dev/null | grep -o 'status: [A-Z]*' | awk '{print $2}')
    got=$(dig @"${r}" +short "${CHALLENGE}" TXT | tr -d '"' | head -1)
    if [[ "${st}" == "NXDOMAIN" ]]; then
      echo "❌ ${r}: NXDOMAIN"
      FAIL=1
    elif [[ "${got}" == "${EXPECTED_TXT}" ]]; then
      echo "✅ ${r}: OK"
    else
      echo "⚠️  ${r}: ${got:-пусто} (status=${st:-?})"
      FAIL=1
    fi
  done
  [[ "${FAIL}" -eq 0 ]] && echo "" && echo "✅ TXT стабилен — жми «Повторить» в Yandex CM"
  exit "${FAIL}"
fi

# --- CNAME mode ---
EXPECTED_CNAME="${2:-}"
CNAME=$(dig +short "${CHALLENGE}" CNAME | head -1 | sed 's/\.$//')

if [[ -z "${CNAME}" ]]; then
  echo "❌ CNAME не найден."
  exit 1
fi
echo "✅ CNAME: ${CNAME}"

if [[ -n "${EXPECTED_CNAME}" && "${CNAME}" != "${EXPECTED_CNAME}" ]]; then
  echo "❌ CNAME не совпадает с Yandex CM"
  exit 1
fi

TXT_DIRECT=$(dig @braelyn.ns.cloudflare.com "${CHALLENGE}" TXT +noall +answer +norecurse 2>/dev/null | grep ' IN TXT ' || true)
if echo "${TXT_DIRECT}" | grep -q ' IN TXT '; then
  echo "❌ На ${CHALLENGE} есть TXT — для CNAME-режима должен быть только CNAME"
  exit 1
fi

FAIL=0
NX=0
for i in $(seq 1 10); do
  st=$(dig @8.8.8.8 "${CHALLENGE}" CNAME +comments 2>/dev/null | grep -o 'status: [A-Z]*' | awk '{print $2}')
  [[ "${st}" == "NXDOMAIN" ]] && ((NX++)) || true
done
if [[ "${NX}" -gt 0 ]]; then
  echo "❌ Google DNS 8.8.8.8: NXDOMAIN в ${NX}/10 проверок — CNAME нестабилен"
  echo ""
  echo "→ Переходи на TXT-валидацию (см. /root/SvoyVPN/docs/yandex-cdn-acme-txt-fix.md)"
  exit 1
fi

for r in 1.1.1.1 8.8.8.8 77.88.8.8; do
  got=$(dig @"${r}" +short "${CHALLENGE}" CNAME | head -1 | sed 's/\.$//')
  if [[ "${got}" == "${CNAME}" ]]; then
    echo "✅ resolver ${r}: OK"
  else
    echo "⚠️  resolver ${r}: ${got:-пусто}"
    FAIL=1
  fi
done

exit "${FAIL}"
