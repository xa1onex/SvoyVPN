"""
eSIM Access API (https://docs.esimaccess.com/) — каталог, заказ, выдача QR.

Режимы:
  ESIM_MODE=test  — без внешнего API, фиктивные страны/тарифы/QR.
  ESIM_MODE=live  — реальные вызовы api.esimaccess.com.

Секреты только из окружения (не хранить в репозитории):
  ESIM_RT_ACCESS_CODE, ESIM_RT_SECRET_KEY
  (алиасы: ESIM_ACCESS_CODE, ESIM_SECRET_KEY)

Цена для списания с user_balances (копейки RUB):
  retailPrice из API в «микро-долларах» / ESIM_PRICE_SCALE (по умолчанию 1000 = 0.001 USD за единицу)
  × ESIM_USD_TO_RUB × 100 × ESIM_RETAIL_MULTIPLIER
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import uuid
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


def _cfg_mode() -> str:
    return (os.environ.get("ESIM_MODE") or "test").strip().lower()


def _cfg_base() -> str:
    return (os.environ.get("ESIM_API_BASE") or "https://api.esimaccess.com").rstrip("/")


def _cfg_headers() -> dict[str, str]:
    code = os.environ.get("ESIM_RT_ACCESS_CODE") or os.environ.get("ESIM_ACCESS_CODE") or ""
    secret = os.environ.get("ESIM_RT_SECRET_KEY") or os.environ.get("ESIM_SECRET_KEY") or ""
    return {
        "Content-Type": "application/json",
        "RT-AccessCode": code.strip(),
        "RT-SecretKey": secret.strip(),
    }


def _price_scale() -> float:
    try:
        return float(os.environ.get("ESIM_PRICE_SCALE") or "1000")
    except ValueError:
        return 1000.0


def _usd_to_rub() -> float:
    try:
        return float(os.environ.get("ESIM_USD_TO_RUB") or "100")
    except ValueError:
        return 100.0


def _retail_multiplier() -> float:
    try:
        return float(os.environ.get("ESIM_RETAIL_MULTIPLIER") or "1.0")
    except ValueError:
        return 1.0


def package_sale_price_kopecks(pkg: dict[str, Any]) -> int:
    """Стоимость для пользователя в копейках RUB по полю retailPrice (или price)."""
    retail = pkg.get("retailPrice")
    if retail is None:
        retail = pkg.get("price") or 0
    try:
        retail_i = int(retail)
    except (TypeError, ValueError):
        retail_i = 0
    usd = retail_i / _price_scale()
    rub = usd * _usd_to_rub() * _retail_multiplier()
    k = int(round(rub * 100))
    return max(100, k)  # минимум 1 ₽


def _lpa_string(smdp: str, activation: str) -> str:
    smdp = (smdp or "").strip()
    activation = (activation or "").strip()
    if not smdp or not activation:
        return ""
    return f"LPA:1${smdp}${activation}"


def _qr_png_base64(data: str) -> str:
    if not (data or "").strip():
        return ""
    try:
        import segno  # type: ignore

        buf = io.BytesIO()
        segno.make(data, error="m").save(buf, kind="png", scale=6, border=2)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except ModuleNotFoundError:
        logger.warning(
            "Пакет segno не установлен — QR для eSIM не генерируется. "
            "На сервере: pip install segno  (или pip install -r requirements.txt)"
        )
        return ""
    except Exception as e:
        logger.warning("QR generation failed: %s", e)
        return ""


def _extract_esim_row(esim: dict[str, Any]) -> dict[str, Any]:
    """Достаём поля из одной записи esimList (имена полей могут отличаться)."""
    smdp = (
        esim.get("smdpAddress")
        or esim.get("manualSmdpAddress")
        or esim.get("smdp")
        or ""
    )
    ac = (
        esim.get("ac")
        or esim.get("activationCode")
        or esim.get("matchingId")
        or esim.get("manualMatchingId")
        or ""
    )
    if isinstance(smdp, str):
        smdp = smdp.strip()
    else:
        smdp = str(smdp or "")
    if isinstance(ac, str):
        ac = ac.strip()
    else:
        ac = str(ac or "")
    qr_url = esim.get("qrCodeUrl") or esim.get("qrUrl") or ""
    lpa = _lpa_string(smdp, ac)
    qr_b64 = ""
    if lpa:
        qr_b64 = _qr_png_base64(lpa)
    return {
        "smdpAddress": smdp,
        "activationCode": ac,
        "lpa": lpa,
        "qrCodeUrl": qr_url,
        "qrImagePngBase64": qr_b64,
        "iccid": esim.get("iccid") or esim.get("iccidNumber"),
        "esimStatus": esim.get("esimStatus"),
        "orderNo": esim.get("orderNo"),
    }


async def _post_json(session: aiohttp.ClientSession, path: str, body: dict) -> dict[str, Any]:
    url = f"{_cfg_base()}{path}"
    text = ""
    status = 0
    try:
        async with session.post(
            url,
            headers=_cfg_headers(),
            json=body,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            status = resp.status
            text = await resp.text()
    except Exception as e:
        logger.warning("eSIM API request failed %s: %s", path, e)
        return {"success": False, "errorMsg": str(e)}
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        logger.error("eSIM API non-JSON: %s %s", status, text[:500])
        return {"success": False, "errorMsg": "invalid_json", "raw": text[:500]}


async def api_location_list(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    data = await _post_json(session, "/api/v1/open/location/list", {})
    if not data.get("success"):
        logger.warning("location/list failed: %s", data.get("errorMsg"))
        return []
    obj = data.get("obj") or {}
    return list(obj.get("locationList") or [])


async def api_package_list(session: aiohttp.ClientSession, location_code: str) -> list[dict[str, Any]]:
    body = {"locationCode": location_code, "type": ""}
    data = await _post_json(session, "/api/v1/open/package/list", body)
    if not data.get("success"):
        logger.warning("package/list failed: %s", data.get("errorMsg"))
        return []
    obj = data.get("obj") or {}
    return list(obj.get("packageList") or [])


async def api_esim_order(
    session: aiohttp.ClientSession, transaction_id: str, package_code: str, count: int = 1
) -> dict[str, Any]:
    body = {
        "transactionId": transaction_id,
        "packageInfoList": [{"packageCode": package_code, "count": int(count)}],
    }
    return await _post_json(session, "/api/v1/open/esim/order", body)


async def api_esim_query(session: aiohttp.ClientSession, batch_order_no: str) -> list[dict[str, Any]]:
    body = {"batchOrderNo": batch_order_no, "pager": {"pageNum": 1, "pageSize": 20}}
    data = await _post_json(session, "/api/v1/open/esim/query", body)
    if not data.get("success"):
        return []
    obj = data.get("obj") or {}
    return list(obj.get("esimList") or [])


def _parse_batch_no(order_resp: dict[str, Any]) -> Optional[str]:
    obj = order_resp.get("obj")
    if isinstance(obj, dict):
        for key in ("batchOrderNo", "orderNo", "batchNo"):
            v = obj.get(key)
            if v:
                return str(v)
        # иногда список заказов
        lst = obj.get("orderList") or obj.get("esimOrderList")
        if isinstance(lst, list) and lst:
            first = lst[0]
            if isinstance(first, dict):
                for key in ("batchOrderNo", "orderNo"):
                    v = first.get(key)
                    if v:
                        return str(v)
    return None


async def poll_esim_list(
    session: aiohttp.ClientSession, batch_order_no: str, attempts: int = 8, delay_sec: float = 1.2
) -> list[dict[str, Any]]:
    for i in range(attempts):
        rows = await api_esim_query(session, batch_order_no)
        if rows:
            return rows
        await asyncio.sleep(delay_sec * (1 + 0.15 * i))
    return []


# ─── TEST catalog (ESIM_MODE=test) ───────────────────────────────────────────

TEST_LOCATIONS = [
    {"code": "DE", "name": "Germany", "type": 1},
    {"code": "ES", "name": "Spain", "type": 1},
    {"code": "FR", "name": "France", "type": 1},
    {"code": "IT", "name": "Italy", "type": 1},
    {"code": "US", "name": "United States", "type": 1},
]

TEST_PACKAGES_BY_LOCATION = {
    "DE": [
        {
            "packageCode": "TEST_DE_1GB_7",
            "name": "Germany 1 GB — 7 days",
            "retailPrice": 10,
            "price": 400,
            "currencyCode": "USD",
            "volume": 1073741824,
            "duration": 7,
            "durationUnit": "DAY",
            "locationCode": "DE",
            "description": "Тестовый тариф",
        },
        {
            "packageCode": "TEST_DE_3GB_30",
            "name": "Germany 3 GB — 30 days",
            "retailPrice": 2200,
            "price": 1100,
            "currencyCode": "USD",
            "volume": 3221225472,
            "duration": 30,
            "durationUnit": "DAY",
            "locationCode": "DE",
            "description": "Тестовый тариф",
        },
    ],
    "ES": [
        {
            "packageCode": "TEST_ES_5GB_14",
            "name": "Spain 5 GB — 14 days",
            "retailPrice": 1500,
            "price": 750,
            "currencyCode": "USD",
            "volume": 5368709120,
            "duration": 14,
            "durationUnit": "DAY",
            "locationCode": "ES",
            "description": "Тестовый тариф",
        },
    ],
}


def test_locations() -> list[dict[str, Any]]:
    return [dict(x) for x in TEST_LOCATIONS]


def test_packages(location_code: str) -> list[dict[str, Any]]:
    code = (location_code or "").upper()
    packs = TEST_PACKAGES_BY_LOCATION.get(code)
    if not packs:
        return []
    return [dict(p) for p in packs]


def test_fake_delivery(package_code: str) -> dict[str, Any]:
    smdp = "rsp.test.esimaccess.demo"
    ac = f"TEST-{package_code}-AC-{uuid.uuid4().hex[:8].upper()}"
    lpa = _lpa_string(smdp, ac)
    return {
        "smdpAddress": smdp,
        "activationCode": ac,
        "lpa": lpa,
        "qrCodeUrl": "",
        "qrImagePngBase64": _qr_png_base64(lpa) if lpa else "",
        "iccid": None,
        "esimStatus": "TEST",
        "orderNo": f"TEST_{uuid.uuid4().hex[:12]}",
    }


def live_credentials_ok() -> bool:
    h = _cfg_headers()
    return bool(h["RT-AccessCode"] and h["RT-SecretKey"])


async def public_locations() -> list[dict[str, Any]]:
    if _cfg_mode() == "live":
        if not live_credentials_ok():
            logger.error("ESIM_MODE=live but ESIM_RT_ACCESS_CODE / ESIM_RT_SECRET_KEY missing")
            return []
        async with aiohttp.ClientSession() as session:
            return await api_location_list(session)
    return test_locations()


async def public_packages(location_code: str) -> list[dict[str, Any]]:
    code = (location_code or "").strip().upper()
    if not code:
        return []
    if _cfg_mode() == "live":
        if not live_credentials_ok():
            return []
        async with aiohttp.ClientSession() as session:
            raw = await api_package_list(session, code)
            out = []
            for p in raw:
                d = dict(p)
                d["salePriceKopecks"] = package_sale_price_kopecks(p)
                out.append(d)
            return out
    out = []
    for p in test_packages(code):
        d = dict(p)
        d["salePriceKopecks"] = package_sale_price_kopecks(d)
        out.append(d)
    return out


async def fulfill_order_live(transaction_id: str, package_code: str) -> tuple[bool, str, dict[str, Any]]:
    """LIVE: создать заказ у провайдера и вернуть данные eSIM. При ошибке: ok=False."""
    if not live_credentials_ok():
        return False, "eSIM API credentials not configured", {}
    async with aiohttp.ClientSession() as session:
        order_resp = await api_esim_order(session, transaction_id, package_code, 1)
        if not order_resp.get("success"):
            msg = order_resp.get("errorMsg") or order_resp.get("errorCode") or "order_failed"
            return False, str(msg), {"providerResponse": order_resp}
        batch_no = _parse_batch_no(order_resp)
        if not batch_no:
            return False, "batchOrderNo missing in provider response", {"providerResponse": order_resp}
        rows = await poll_esim_list(session, batch_no)
        if not rows:
            return False, "eSIM not ready yet (empty esimList); try later or wait for webhook", {
                "batchOrderNo": batch_no,
                "providerResponse": order_resp,
            }
        extracted = _extract_esim_row(rows[0])
        extracted["batchOrderNo"] = batch_no
        return True, "", extracted
