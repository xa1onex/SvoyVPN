"""
Массово убрать flow=xtls-rprx-vision у grpc-клиентов на панелях и включить
клиентов, у которых в БД есть активный ключ. Старые ключи создавались с flow
на grpc — из-за этого у части пользователей n/a, у части OK.
"""
import asyncio
import json
import logging

from dotenv import load_dotenv

load_dotenv("/root/SvoyVPN/.env")

import httpx

from bot.database import get_connection

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
log = logging.getLogger("fix_flow")


async def fix_server(server: dict, active_uuids: set[str]) -> tuple[int, int, int]:
    base = server["base_url"].rstrip("/") + "/"
    fixed_flow = 0
    reenabled = 0
    async with httpx.AsyncClient(base_url=base, verify=False, timeout=60, follow_redirects=True) as c:
        r = await c.post("login", data={"username": server["username"], "password": server["password"]})
        if r.status_code not in (200, 302):
            raise RuntimeError(f"login failed {r.status_code}")

        inbounds = (await c.get("panel/api/inbounds/list")).json().get("obj", [])
        ib = next((i for i in inbounds if i.get("id") == server["inbound_id"]), None)
        if not ib:
            raise RuntimeError(f"inbound {server['inbound_id']} not found")

        ss = json.loads(ib.get("streamSettings") or "{}")
        network = (ss.get("network") or "tcp").lower()
        settings = json.loads(ib.get("settings") or "{}")
        clients = settings.get("clients") or []
        if not isinstance(clients, list):
            return 0, 0, 0

        changed = False
        for cl in clients:
            cid = str(cl.get("id") or "").replace("-", "")
            if network == "grpc" and cl.get("flow"):
                cl.pop("flow", None)
                fixed_flow += 1
                changed = True
            elif network == "tcp" and not cl.get("flow"):
                cl["flow"] = "xtls-rprx-vision"
                fixed_flow += 1
                changed = True
            if cid in active_uuids and not cl.get("enable", True):
                cl["enable"] = True
                reenabled += 1
                changed = True

        if not changed:
            return fixed_flow, reenabled, 0

        settings["clients"] = clients
        ib["settings"] = json.dumps(settings, ensure_ascii=False)
        payload = {
            k: ib.get(k)
            for k in (
                "up", "down", "total", "remark", "enable", "expiryTime", "listen",
                "port", "protocol", "settings", "streamSettings", "sniffing", "allocate",
            )
        }
        upd = await c.post(
            f"panel/api/inbounds/update/{server['inbound_id']}",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if upd.status_code != 200 or not upd.json().get("success"):
            raise RuntimeError(f"update failed: {upd.status_code} {upd.text[:120]}")

        rst = await c.post("panel/api/server/restartXrayService")
        restarted = 1 if rst.json().get("success") else 0
        return fixed_flow, reenabled, restarted


async def main() -> None:
    async with get_connection() as conn:
        servers = await conn.fetch(
            """
            SELECT * FROM servers
            WHERE is_active = TRUE AND COALESCE(is_system, FALSE) = FALSE
            ORDER BY id
            """
        )
        rows = await conn.fetch(
            """
            SELECT k.vless_client_id, k.server_id
            FROM vpn_keys k
            JOIN servers s ON s.id = k.server_id
            WHERE k.is_active = TRUE
              AND s.is_active = TRUE
              AND COALESCE(s.is_system, FALSE) = FALSE
              AND (k.expires_at IS NULL OR DATE(k.expires_at) >= CURRENT_DATE)
            """
        )
    by_server: dict[int, set[str]] = {}
    for r in rows:
        by_server.setdefault(int(r["server_id"]), set()).add(str(r["vless_client_id"]).replace("-", ""))

    total_flow = total_en = total_rst = 0
    for s in servers:
        sid = int(s["id"])
        try:
            ff, re, rst = await fix_server(dict(s), by_server.get(sid, set()))
            total_flow += ff
            total_en += re
            total_rst += rst
            if ff or re:
                log.info("#%s %s: fixed_flow=%s reenabled=%s restarted=%s", sid, s["name"][:24], ff, re, rst)
        except Exception as e:
            log.error("#%s %s: %s", sid, s["name"][:24], e)
    print(f"DONE fixed_flow={total_flow} reenabled={total_en} xray_restarts={total_rst}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
