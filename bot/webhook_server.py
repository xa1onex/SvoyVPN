"""
HTTP сервер для обработки вебхуков (YooKassa, Flyer) и subscription endpoint
"""
import json
import logging
import os
import mimetypes
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qs, unquote
import secrets
import hashlib
import bcrypt
import jwt as pyjwt
from aiohttp import web, web_request
from email.utils import formatdate, make_msgid
from aiohttp.web_exceptions import HTTPBadRequest, HTTPNotFound, HTTPMethodNotAllowed
from aiohttp.http_exceptions import BadStatusLine, BadHttpMessage
from aiogram.types import LabeledPrice, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import FlyerConfig, YooKassaConfig
from .database import get_connection, log_subscription_usage
from .subscriptions import create_or_activate_keys_for_all_servers, get_user_subscription_url
from .plans import get_subscription_plans, get_renewal_plans

logger = logging.getLogger(__name__)

# Ensure common static MIME types for miniapp assets.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("application/json", ".map")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("image/x-icon", ".ico")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("application/wasm", ".wasm")


class BadStatusLineFilter(logging.Filter):
    """Фильтр для подавления логов BadStatusLine (сканирование портов)"""
    def filter(self, record):
        message = str(record.getMessage())
        keywords = ['BadStatusLine', 'BadHttpMessage', 'Invalid method encountered', 'Pause on PRI/Upgrade']
        if any(keyword in message for keyword in keywords):
            return False
        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            if exc_type and any(keyword in exc_type.__name__ for keyword in ['BadStatusLine', 'BadHttpMessage']):
                return False
            if exc_value and any(keyword in str(exc_value) for keyword in keywords):
                return False
        return True


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
                "Access-Control-Max-Age": "86400"
            }
        )
    try:
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    except web.HTTPException as ex:
        ex.headers["Access-Control-Allow-Origin"] = "*"
        raise

class WebhookServer:
    """HTTP сервер для вебхуков и subscription endpoint"""
    
    def __init__(
        self,
        flyer_config: FlyerConfig,
        yookassa_config: Optional[YooKassaConfig] = None,
        cryptopay_config = None,
        bot_instance=None,
        yookassa_client=None,
        payment_processor=None,
        admin_ids: list[int] = None
    ):
        self.flyer_config = flyer_config
        self.yookassa_config = yookassa_config
        self.cryptopay_config = cryptopay_config
        self.bot = bot_instance
        self.yookassa_client = yookassa_client
        self.payment_processor = payment_processor
        self.admin_ids = admin_ids or []
        self.app = web.Application(middlewares=[cors_middleware])
        
        # Core routes
        self.app.router.add_get('/', self.root_handler)
        self.app.router.add_get('/sub/{token}', self.handle_subscription)
        self.app.router.add_post('/webhook/flyer', self.handle_flyer_webhook)
        self.app.router.add_get('/webhook/flyer', self.health_check)
        if yookassa_config and yookassa_config.enabled:
            self.app.router.add_post('/webhook/yookassa', self.handle_yookassa_webhook)
            self.app.router.add_post('/webhook/yookassa/', self.handle_yookassa_webhook)
        if cryptopay_config and cryptopay_config.enabled:
            self.app.router.add_post('/webhook/cryptopay', self.handle_cryptopay_webhook)
            self.app.router.add_post('/webhook/cryptopay/', self.handle_cryptopay_webhook)
            self.app.router.add_get('/webhook/cryptopay', self.health_check_cryptopay)
            self.app.router.add_get('/webhook/cryptopay/', self.health_check_cryptopay)
        
        # Deep link routes (for automatic app connection)
        self.app.router.add_get('/apple/{app}/{token}', self.handle_app_connect)
        self.app.router.add_get('/android/{app}/{token}', self.handle_app_connect)
        self.app.router.add_get('/windows/{app}/{token}', self.handle_app_connect)
        self.app.router.add_get('/mac/{app}/{token}', self.handle_app_connect)
        
        # Miniapp routes
        self.app.router.add_get('/miniapp', self.serve_miniapp)
        self.app.router.add_get('/miniapp/', self.serve_miniapp)
        
        # API routes
        api_routes = [
            ('/api/user', self.api_get_user, 'POST'),
            ('/api/user', self.api_get_user_jwt, 'GET'),          # JWT auth (Android)
            ('/api/tariffs', self.api_get_tariffs, 'GET'),
            ('/api/payment-methods', self.api_get_payment_methods, 'GET'),
            ('/api/payment/create', self.api_create_payment, 'POST'),
            ('/api/servers', self.api_get_servers, 'GET'),
            ('/api/ping', self.api_ping_server, 'GET'),
            ('/api/referral', self.api_get_referral, 'GET'),
            ('/api/trial/activate', self.api_activate_trial, 'POST'),
            ('/api/news', self.api_get_news, 'GET'),
            ('/api/news/add', self.api_add_news, 'POST'),
            ('/api/news/delete', self.api_delete_news, 'POST'),
            ('/api/auth/tg-init', self.api_auth_tg_init, 'POST'),
            ('/api/auth/tg-poll', self.api_auth_tg_poll, 'GET'),
            ('/api/auth/email-otp', self.api_auth_email_otp, 'POST'),
            ('/api/auth/register', self.api_auth_register, 'POST'),
            ('/api/auth/login', self.api_auth_login, 'POST'),
            ('/api/auth/reset-otp', self.api_auth_reset_otp, 'POST'),
            ('/api/auth/reset-password', self.api_auth_reset_password, 'POST'),
        ]
        
        for path, handler, method in api_routes:
            if method == 'GET':
                self.app.router.add_get(path, handler)
                self.app.router.add_get('/miniapp' + path, handler)
            else:
                self.app.router.add_post(path, handler)
                self.app.router.add_post('/miniapp' + path, handler)
        
        self.app.router.add_get('/miniapp/news_images/{path:.*}', self.serve_news_image)
        self.app.router.add_get('/miniapp/{path:.*}', self.serve_miniapp_static)
        
        self.app.middlewares.append(self.handle_bad_requests_middleware)
        self.runner = None
        
        # Подавляем шум в логах от сканеров
        for logger_name in ("aiohttp.access", "aiohttp.server"):
            l = logging.getLogger(logger_name)
            l.addFilter(BadStatusLineFilter())

        logger.info(f"WebhookServer initialized with routes (POST): /webhook/flyer, /webhook/yookassa, /webhook/cryptopay")
        logger.info(f"WebhookServer initialized with routes (GET): /, /sub/{{token}}, /api/*, /webhook/cryptopay")
    
    @staticmethod
    def extract_emoji_and_name(full_name: str) -> tuple:
        """Извлекает эмодзи из начала строки и возвращает (эмодзи, чистое_название)"""
        name = (full_name or "").strip()
        if not name:
            return "🌍", ""
            
        # Херистика для флагов (2 региональные буквы) и обычных эмодзи
        first_code = ord(name[0])
        
        # Региональные индикаторы (флаги) — это 2 символа в UTF-16/32
        if 0x1F1E6 <= first_code <= 0x1F1FF and len(name) >= 2:
            return name[0:2], name[2:].strip()
            
        # Другие эмодзи (обычно начинаются за пределами базовой латиницы)
        if first_code > 127:
            return name[0], name[1:].strip()
            
        return "🌍", name


    async def root_handler(self, request: web_request.Request) -> web.Response:
        """Обработчик корневого пути"""
        return web.json_response({"status": "ok", "service": "webhook_server"})
    
    async def health_check(self, request: web_request.Request) -> web.Response:
        """Проверка здоровья сервера"""
        return web.json_response({"status": "ok", "service": "flyer_webhook"})
    
    async def handle_subscription(self, request: web_request.Request) -> web.Response:
        """
        Subscription endpoint: возвращает список VLESS-ссылок по subscription_token
        
        Формат: text/plain, строки vless://... разделённые \n
        """
        token = (request.match_info.get("token") or "").strip()
        logger.info(f"Subscription request received: token={token[:10] if token else 'None'}..., path={request.path_qs}, remote={request.remote}, method={request.method}")
        
        if not token or len(token) < 8:
            logger.warning(f"Invalid token: token={token}, length={len(token) if token else 0}")
            raise HTTPNotFound()
        
        try:
            async with get_connection() as conn:
                user_row = await conn.fetchrow(
                    "SELECT user_id, blacklisted, pay_subscribed, subscription_end FROM users WHERE subscription_token = $1",
                    token
                )
                
                if not user_row:
                    logger.warning(f"User not found for token: {token[:10]}...")
                    raise HTTPNotFound()
                
                if user_row.get("blacklisted"):
                    logger.warning(f"User {user_row['user_id']} is blacklisted")
                    raise HTTPNotFound()
                
                user_id = user_row["user_id"]
                
                # Логируем запрос (User-Agent и IP)
                user_agent = request.headers.get("User-Agent", "Unknown")
                ip_address = request.headers.get("X-Forwarded-For", request.remote or "Unknown")
                if "," in ip_address: ip_address = ip_address.split(",")[0].strip()
                await log_subscription_usage(user_id, user_agent, ip_address)
                
                # Проверяем активность подписки
                is_active = await conn.fetchval('''
                    SELECT CASE
                        WHEN pay_subscribed = TRUE
                         AND subscription_end IS NOT NULL
                         AND DATE(subscription_end) >= CURRENT_DATE
                        THEN TRUE ELSE FALSE END
                    FROM users WHERE user_id = $1
                ''', user_id)
                
                # Получаем ключи и информацию о сервере для сортировки
                keys_data = await conn.fetch('''
                    SELECT DISTINCT ON (k.server_id) 
                        k.vless_link, k.server_id, s.display_order, s.id as sid
                    FROM vpn_keys k
                    INNER JOIN servers s ON k.server_id = s.id
                    WHERE k.user_id = $1 
                      AND k.is_active = TRUE
                      AND s.is_active = TRUE
                      AND (k.expires_at IS NULL OR DATE(k.expires_at) >= CURRENT_DATE)
                    ORDER BY k.server_id, k.id ASC
                ''', user_id)
                
                # Сортируем ключи по display_order, затем по id сервера
                keys = sorted(keys_data, key=lambda x: (x.get('display_order', 100), x.get('sid', 0)))
                
                # Формируем expire timestamp
                subscription_end = user_row.get("subscription_end")
                expire_ts = 0
                if subscription_end and is_active:
                    try:
                        if isinstance(subscription_end, str):
                            dt = datetime.strptime(subscription_end.split()[0], "%Y-%m-%d")
                        else:
                            dt = subscription_end
                        expire_ts = int(dt.replace(hour=23, minute=59, second=59).timestamp())
                    except:
                        pass
                
                # ✅ Проверяем наличие новых серверов без ключей и создаём их автоматически
                if is_active:
                    # Проверяем, есть ли активные серверы без ключей
                    servers_without_keys = await conn.fetch('''
                        SELECT s.id
                        FROM servers s
                        WHERE s.is_active = TRUE
                          AND NOT EXISTS (
                              SELECT 1 FROM vpn_keys k
                              WHERE k.server_id = s.id
                                AND k.user_id = $1
                                AND k.is_active = TRUE
                                AND (k.expires_at IS NULL OR DATE(k.expires_at) >= CURRENT_DATE)
                          )
                    ''', user_id)
                    
                    if servers_without_keys:
                        logger.info(f"User {user_id} has {len(servers_without_keys)} servers without keys, creating...")
                        try:
                            # Создаём ключи вне транзакции (используем отдельное соединение)
                            from .subscriptions import create_or_activate_keys_for_all_servers
                            await create_or_activate_keys_for_all_servers(user_id)
                            # Повторно запрашиваем ключи и информацию о сервере для сортировки
                            keys_data = await conn.fetch('''
                                SELECT DISTINCT ON (k.server_id) 
                                    k.vless_link, k.server_id, s.display_order, s.id as sid
                                FROM vpn_keys k
                                INNER JOIN servers s ON k.server_id = s.id
                                WHERE k.user_id = $1 
                                  AND k.is_active = TRUE
                                  AND s.is_active = TRUE
                                  AND (k.expires_at IS NULL OR DATE(k.expires_at) >= CURRENT_DATE)
                                ORDER BY k.server_id, k.id ASC
                            ''', user_id)
                            
                            # Сортируем ключи по display_order, затем по id сервера
                            keys = sorted(keys_data, key=lambda x: (x.get('display_order', 100), x.get('sid', 0)))
                        except Exception as e:
                            logger.error(f"Failed to auto-create keys for user {user_id}: {e}")
                
                # Формируем ответ
                body = "\n".join([k["vless_link"] for k in keys if k.get("vless_link")])
                
                logger.info(f"Returning subscription for user {user_id}: {len(keys)} keys, active={is_active}")
                
                # Формируем объявление (одной строкой для заголовка HTTP)
                announce_text = (
                    "При проблемах с интернетом используйте страны со значком 🆓. "
                    "Если что-то не работает или тормозит — обновите подписку кнопкой 🔄"
                )

                headers = {
                    "Cache-Control": "no-store",
                    "Content-Disposition": 'attachment; filename="SvoyVPN"',
                    "profile-title": "Svoy VPN",
                    "profile-update-interval": "4",
                    "support-url": "https://t.me/majorka_wy",
                    "profile-web-page-url": "https://t.me/SvoyVPN_robot",
                    "announce": announce_text,
                    "subscription-userinfo": f"upload=0; download=0; total=0; expire={expire_ts}" if is_active else "Inactive"
                }
                
                return web.Response(
                    status=200,
                    text=body,
                    content_type="text/plain",
                    charset="utf-8",
                    headers=headers
                )
        except (HTTPNotFound, HTTPBadRequest):
            raise
        except Exception as e:
            logger.error(f"Internal error in handle_subscription for token {token[:8]}... : {e}", exc_info=True)
            return web.json_response({"error": "Internal Server Error", "details": str(e)}, status=500)
    
    async def handle_app_connect(self, request: web_request.Request) -> web.Response:
        """
        Обработчик диплинков для автоматического подключения приложения.
        Принимает /{device}/{app}/{token} и редиректит в приложение.
        """
        app_id = request.match_info.get("app", "").lower()
        token = request.match_info.get("token", "").strip()
        
        if not token or len(token) < 8:
            logger.warning(f"App connect: Invalid token '{token}'")
            raise HTTPNotFound()
            
        # Определяем базовый URL для подписки
        # Используем значение из конфига или текущий хост
        base_url = "https://xdoublegroup.online" # Дефолт
        if hasattr(self.flyer_config, 'subscription_base_url') and self.flyer_config.subscription_base_url:
            base_url = self.flyer_config.subscription_base_url.rstrip('/')
        elif request.host:
            protocol = "https" if request.secure else "http"
            base_url = f"{protocol}://{request.host}"
            
        sub_url = f"{base_url}/sub/{token}"
        
        # Маппинг схем приложений
        schemes = {
            "happ": f"happ://import/{sub_url}",
            "hiddify": f"hiddify://import/{sub_url}",
            "v2raytun": f"v2raytun://import/{sub_url}",
            "v2rayng": f"v2rayng://install-config?url={sub_url}",
            "v2rayn": f"v2rayn://install-config?url={sub_url}",
            "streisand": f"streisand://import/{sub_url}",
            "shadowrocket": f"shadowrocket://add/{sub_url}",
            "singbox": f"sing-box://import-remote?url={sub_url}",
        }
        
        deep_link = schemes.get(app_id, f"{app_id}://import/{sub_url}")
        
        logger.info(f"Deep link redirect: app={app_id}, token={token[:8]}... -> {deep_link}")
        
        # Используем HTML-страницу для редиректа, так как прямые 302 на кастомные схемы
        # часто блокируются мобильными браузерами.
        html = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SvoyVPN — Подключение...</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
            background-color: #18222d;
            color: white;
            text-align: center;
            padding: 20px;
        }}
        .logo-box {{
            margin-bottom: 30px;
        }}
        .loader {{
            border: 3px solid rgba(255, 255, 255, 0.1);
            border-left: 3px solid #3aa8fc;
            border-radius: 50%;
            width: 50px;
            height: 50px;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }}
        @keyframes spin {{
            0% {{ transform: rotate(0deg); }}
            100% {{ transform: rotate(360deg); }}
        }}
        h2 {{ font-weight: 600; margin-bottom: 10px; }}
        p {{ color: #8e8e93; font-size: 15px; max-width: 280px; line-height: 1.4; }}
        .btn {{
            display: inline-block;
            background-color: #3aa8fc;
            color: white;
            border: none;
            padding: 16px 32px;
            border-radius: 14px;
            font-size: 16px;
            font-weight: 600;
            margin-top: 30px;
            text-decoration: none;
            transition: transform 0.1s;
            -webkit-tap-highlight-color: transparent;
        }}
        .btn:active {{ transform: scale(0.96); }}
    </style>
</head>
<body>
    <div class="logo-box">
        <div class="loader"></div>
    </div>
    <h2>Открываем приложение...</h2>
    <p>Если приложение не открылось автоматически, нажмите кнопку ниже:</p>
    
    <a href="{deep_link}" class="btn">ПОДКЛЮЧИТЬ VPN</a>
    
    <script>
        // Пытаемся выполнить автоматический переход через небольшую паузу
        setTimeout(function() {{
            window.location.href = "{deep_link}";
        }}, 500);
        
        // Для некоторых браузеров может потребоваться клик, поэтому кнопка обязательна
    </script>
</body>
</html>
        """
        
        return web.Response(text=html, content_type='text/html', charset='utf-8')
    
    @web.middleware
    async def handle_bad_requests_middleware(self, request: web_request.Request, handler):
        """Middleware для обработки некорректных HTTP-запросов"""
        logger.info(f"Request received: {request.method} {request.path_qs} from {request.remote}")
        try:
            response = await handler(request)
            logger.info(f"Response: {request.method} {request.path_qs} -> {response.status}")
            return response
        except (BadStatusLine, BadHttpMessage, HTTPBadRequest) as e:
            logger.debug(f"Invalid HTTP request from {request.remote}: {type(e).__name__}")
            return web.Response(status=400, text="Bad Request")
        except (HTTPNotFound, HTTPMethodNotAllowed) as e:
            logger.warning(f"Not found: {request.method} {request.path_qs} - {type(e).__name__}")
            if isinstance(e, HTTPNotFound):
                return web.json_response({"status": "error", "message": "Not Found"}, status=404)
            else:
                return web.json_response({"status": "error", "message": "Method Not Allowed"}, status=405)
        except Exception as e:
            logger.error(f"Error handling request {request.path_qs}: {e}", exc_info=True)
            raise
    
    async def handle_flyer_webhook(self, request: web_request.Request) -> web.Response:
        """Обработчик вебхуков от Flyer Service"""
        try:
            data = await request.json()
            logger.info(f"Received Flyer webhook: {json.dumps(data, ensure_ascii=False)}")
            
            event_type = data.get("type")
            key_number = data.get("key_number")
            event_data = data.get("data", {})
            
            if event_type == "test":
                logger.info(f"Test webhook received for key_number={key_number}")
            elif event_type == "sub_completed":
                user_id = event_data.get("user_id")
                await self.handle_flyer_sub_completed(user_id, key_number)
            elif event_type == "new_status":
                # Сохраняем событие
                await self.save_flyer_event(event_type, event_data, key_number)
            else:
                logger.warning(f"Unknown Flyer event type: {event_type}")
            
            return web.json_response({"status": True})
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in Flyer webhook: {e}")
            return web.json_response({"status": False, "error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"Error processing Flyer webhook: {e}", exc_info=True)
            return web.json_response({"status": False, "error": str(e)}, status=500)
    
    async def handle_flyer_sub_completed(self, user_id: int, key_number: Optional[int] = None):
        """Обработка события sub_completed - добавляет 1 день к подписке"""
        try:
            logger.info(f"User {user_id} completed Flyer subscription (key_number={key_number})")
            
            async with get_connection() as conn:
                user_exists = await conn.fetchval('SELECT user_id FROM users WHERE user_id = $1', user_id)
                
                if user_exists:
                    # Добавляем 1 день к подписке
                    await conn.execute('''
                        UPDATE users 
                        SET subscription_end = subscription_end + INTERVAL '1 day'
                        WHERE user_id = $1 
                          AND pay_subscribed = TRUE 
                          AND subscription_end IS NOT NULL
                    ''', user_id)
                    logger.info(f"Added 1 day to subscription for user {user_id}")
                    
        except Exception as e:
            logger.error(f"Error handling Flyer sub_completed: {e}", exc_info=True)
    
    async def save_flyer_event(self, event_type: str, event_data: dict, key_number: Optional[int] = None):
        """Сохраняет событие Flyer в БД"""
        try:
            async with get_connection() as conn:
                # Проверяем наличие таблицы flyer_webhook_events
                table_exists = await conn.fetchval('''
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = 'flyer_webhook_events'
                    )
                ''')
                
                if table_exists:
                    await conn.execute('''
                        INSERT INTO flyer_webhook_events 
                        (event_type, user_id, key_number, event_data, created_at)
                        VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                    ''', event_type, event_data.get("user_id"), key_number, json.dumps(event_data))
        except Exception as e:
            logger.error(f"Error saving Flyer event: {e}", exc_info=True)
    
    async def handle_yookassa_webhook(self, request: web_request.Request) -> web.Response:
        """Обработчик вебхуков от YooKassa"""
        try:
            data = await request.json()
            logger.info(f"Received YooKassa webhook: {json.dumps(data, ensure_ascii=False)}")
            
            event = data.get("event")
            payment_obj = data.get("object", {}) or {}
            
            if event == "payment.succeeded":
                payment_id = payment_obj.get("id")
                status = payment_obj.get("status")
                paid = payment_obj.get("paid", False)
                metadata = payment_obj.get("metadata") or {}
                
                if status == "succeeded" and paid and payment_id:
                    if self.payment_processor:
                        await self.payment_processor(
                            payment_id=payment_id,
                            payment_obj=payment_obj,
                            metadata=metadata
                        )
                else:
                    logger.warning(f"YooKassa payment not succeeded: payment_id={payment_id}, status={status}, paid={paid}")
            else:
                logger.info(f"YooKassa webhook event '{event}' ignored")
            
            return web.json_response({"status": "ok"})
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in YooKassa webhook: {e}")
            return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"Error processing YooKassa webhook: {e}", exc_info=True)
            return web.json_response({"status": "error", "message": str(e)}, status=500)
    
    async def health_check_cryptopay(self, request: web_request.Request) -> web.Response:
        """Health check for Crypto Pay webhook endpoint"""
        logger.info(f"Crypto Pay health check request from {request.remote}")
        return web.Response(text="Crypto Pay Webhook Endpoint is ALIVE", status=200)
    
    async def handle_cryptopay_webhook(self, request: web_request.Request) -> web.Response:
        """Обработчик вебхуков от Crypto Pay"""
        logger.info(f"Crypto Pay webhook request received from {request.remote}")
        try:
            body = await request.read()
            signature = request.headers.get('crypto-pay-api-signature')
            
            logger.debug(f"Crypto Pay body: {body.decode('utf-8', errors='ignore')}")
            logger.debug(f"Crypto Pay signature header: {signature}")
            
            if not signature or not self.cryptopay_config or not self.cryptopay_config.api_token:
                logger.warning(f"Unauthorized Crypto Pay webhook attempt. Signature present: {bool(signature)}, Token present: {bool(self.cryptopay_config and self.cryptopay_config.api_token)}")
                return web.Response(status=401, text="Unauthorized")
                
            import hashlib
            import hmac
            secret = hashlib.sha256(self.cryptopay_config.api_token.encode()).digest()
            calculated_hmac = hmac.new(secret, body, hashlib.sha256).hexdigest()
            
            if calculated_hmac != signature.lower():
                logger.warning(f"Invalid Crypto Pay signature.")
                logger.warning(f"  Received (header): {signature}")
                logger.warning(f"  Calculated (HMAC): {calculated_hmac}")
                logger.warning(f"  Token used (first 4): {self.cryptopay_config.api_token[:4] if self.cryptopay_config.api_token else 'NONE'}")
                return web.Response(status=401, text="Unauthorized")
            
            data = json.loads(body.decode('utf-8'))
            logger.info(f"Received VALID Crypto Pay webhook: {json.dumps(data, ensure_ascii=False)}")
            
            update_type = data.get("update_type")
            payload = data.get("payload", {})
            
            if update_type == "invoice_paid":
                status = payload.get("status")
                if status == "paid":
                    invoice_id = payload.get("invoice_id")
                    meta_payload = payload.get("payload", "")
                    try:
                        metadata = json.loads(meta_payload) if meta_payload else {}
                    except:
                        if meta_payload and ":" in meta_payload:
                            parts = meta_payload.split(":")
                            metadata = {
                                "user_id": int(parts[0]),
                                "plan_id": parts[1],
                                "method_id": parts[2] if len(parts) > 2 else "cryptopay"
                            }
                        else:
                            metadata = {}
                        
                    if self.payment_processor:
                        await self.payment_processor(
                            payment_id=str(invoice_id),
                            payment_obj=payload,
                            metadata=metadata
                        )
                else:
                    logger.warning(f"Crypto Pay invoice not paid: {payload}")
            else:
                logger.info(f"Crypto Pay webhook update_type '{update_type}' ignored")
                
            return web.json_response({"status": "ok"})
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in Crypto Pay webhook: {e}")
            return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"Error processing Crypto Pay webhook: {e}", exc_info=True)
            return web.json_response({"status": "error", "message": str(e)}, status=500)
    
    async def serve_miniapp(self, request: web_request.Request) -> web.Response:
        """Отдает главную страницу miniapp"""
        miniapp_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'miniapp', 'index.html')
        try:
            with open(miniapp_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return web.Response(text=content, content_type='text/html', charset='utf-8')
        except FileNotFoundError:
            logger.error(f"Miniapp file not found: {miniapp_path}")
            return web.Response(text="Miniapp not found", status=404)
        except Exception as e:
            logger.error(f"Error serving miniapp: {e}")
            return web.Response(text="Internal server error", status=500)
    
    async def serve_miniapp_static(self, request: web_request.Request) -> web.Response:
        """Отдает статические файлы miniapp (CSS, JS)"""
        path = request.match_info.get('path', '')
        if not path:
            return await self.serve_miniapp(request)
        
        # Убираем начальный слэш, если есть
        path = path.lstrip('/')
        
        miniapp_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'miniapp')
        file_path = os.path.join(miniapp_dir, path)
        
        # Нормализуем путь для безопасности
        file_path = os.path.normpath(file_path)
        miniapp_dir = os.path.normpath(miniapp_dir)
        
        # Проверяем безопасность пути
        if not os.path.abspath(file_path).startswith(os.path.abspath(miniapp_dir)):
            logger.warning(f"Forbidden path access attempt: {path}")
            return web.Response(text="Forbidden", status=403)
        
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            logger.debug(f"Serving static file: {path} ({len(content)} bytes)")
            guessed_type, _ = mimetypes.guess_type(file_path)
            if not guessed_type:
                if file_path.endswith('.css'):
                    guessed_type = 'text/css'
                elif file_path.endswith('.js'):
                    guessed_type = 'application/javascript'
                elif file_path.endswith('.svg'):
                    guessed_type = 'image/svg+xml'
                elif file_path.endswith('.png'):
                    guessed_type = 'image/png'
                elif file_path.endswith('.woff2'):
                    guessed_type = 'font/woff2'
                elif file_path.endswith('.woff'):
                    guessed_type = 'font/woff'
                elif file_path.endswith('.ttf'):
                    guessed_type = 'font/ttf'
            content_type = guessed_type or "application/octet-stream"
            # Add charset for text-like assets.
            if content_type.startswith("text/") or content_type in {
                "application/javascript",
                "text/javascript",
                "application/json",
                "image/svg+xml",
                "application/xml",
            }:
                return web.Response(body=content, content_type=content_type, charset="utf-8")
            return web.Response(body=content, content_type=content_type)
        except FileNotFoundError:
            logger.error(f"Static file not found: {file_path} (requested path: {path})")
            return web.Response(text=f"File not found: {path}", status=404)
        except Exception as e:
            logger.error(f"Error serving static file {path}: {e}", exc_info=True)
            return web.Response(text="Internal server error", status=500)
    
    def verify_telegram_webapp_data(self, init_data: str, bot_token: str) -> bool:
        """Проверяет подлинность данных от Telegram WebApp"""
        try:
            from urllib.parse import parse_qs, unquote
            import hmac
            import hashlib
            
            # Парсим init_data
            data_dict = {}
            for item in init_data.split('&'):
                if '=' in item:
                    key, value = item.split('=', 1)
                    data_dict[key] = unquote(value)
            
            # Извлекаем hash
            received_hash = data_dict.pop('hash', '')
            if not received_hash:
                return False
            
            # Создаем строку для проверки
            data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(data_dict.items()))
            
            # Вычисляем секретный ключ
            secret_key = hmac.new(
                b"WebAppData",
                bot_token.encode(),
                hashlib.sha256
            ).digest()
            
            # Вычисляем hash
            calculated_hash = hmac.new(
                secret_key,
                data_check_string.encode(),
                hashlib.sha256
            ).hexdigest()
            
            return calculated_hash == received_hash
            
        except Exception as e:
            logger.error(f"Error verifying Telegram WebApp data: {e}")
            return False
    
    def parse_telegram_init_data(self, init_data: str) -> dict:
        """Парсит init_data от Telegram WebApp"""
        result = {}
        try:
            from urllib.parse import unquote
            for item in init_data.split('&'):
                if '=' in item:
                    key, value = item.split('=', 1)
                    result[key] = unquote(value)
        except Exception as e:
            logger.error(f"Error parsing init_data: {e}")
        return result
    
    async def api_get_user(self, request: web_request.Request) -> web.Response:
        """API: Получить данные пользователя"""
        try:
            data = await request.json()
            init_data = data.get('initData', '')
            
            if not init_data:
                return web.json_response({"error": "initData required"}, status=400)
            
            # Проверяем подлинность данных
            if not self.bot:
                return web.json_response({"error": "Bot not initialized"}, status=500)
            
            bot_token = self.bot.token
            if not self.verify_telegram_webapp_data(init_data, bot_token):
                return web.json_response({"error": "Invalid initData"}, status=403)
            
            # Парсим данные пользователя
            parsed_data = self.parse_telegram_init_data(init_data)
            user_str = parsed_data.get('user', '{}')
            user_data = json.loads(user_str) if user_str else {}
            user_id = int(user_data.get('id', 0))
            
            if not user_id:
                return web.json_response({"error": "User ID not found"}, status=400)
            
            # Получаем данные пользователя из БД
            async with get_connection() as conn:
                user = await conn.fetchrow(
                    "SELECT user_id, pay_subscribed, subscription_end, subscription_token, trial_used FROM users WHERE user_id = $1",
                    user_id
                )
                
                if not user:
                    return web.json_response({"error": "User not found"}, status=404)
                
                # Проверяем активность подписки
                is_active = False
                end_date = None
                if user['pay_subscribed'] and user['subscription_end']:
                    end_date = user['subscription_end']
                    if isinstance(end_date, str):
                        end_date = datetime.strptime(end_date.split()[0], "%Y-%m-%d").date()
                    elif hasattr(end_date, 'date'):
                        # Если это datetime.datetime, преобразуем в date
                        end_date = end_date.date()
                    # Теперь end_date точно date, сравниваем с date
                    is_active = end_date >= datetime.now().date()
                
                # Получаем ссылку на подписку
                from .subscriptions import get_user_subscription_url
                subscription_url = await get_user_subscription_url(user_id, None)
                
                # Форматируем end_date для JSON
                end_date_str = None
                if end_date:
                    if hasattr(end_date, 'isoformat'):
                        end_date_str = end_date.isoformat()
                    elif isinstance(end_date, str):
                        end_date_str = end_date
                
                # --- Fetch profile photo via Bot API --- #
                photo_url_fetched = user_data.get('photo_url', '')
                if not photo_url_fetched:
                    try:
                        if self.bot:
                            photos_result = await self.bot.get_user_profile_photos(user_id=user_id, limit=1)
                            if photos_result and photos_result.photos and len(photos_result.photos) > 0:
                                sizes = photos_result.photos[0]  # list of PhotoSize
                                if sizes and len(sizes) > 0:
                                    biggest = sizes[-1]
                                    file_obj = await self.bot.get_file(biggest.file_id)
                                    if file_obj and file_obj.file_path:
                                        photo_url_fetched = f"https://api.telegram.org/file/bot{self.bot.token}/{file_obj.file_path}"
                                        logger.info(f"Fetched profile photo for user {user_id}: {photo_url_fetched}")
                    except Exception as ex:
                        logger.warning(f"Could not fetch profile photo for user {user_id}: {ex}", exc_info=True)

                # Trial logic
                trial_settings = await conn.fetchrow('SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1')
                trial_days = trial_settings['days'] if trial_settings and trial_settings['days'] else 0
                trial_available = (user['trial_used'] is False) and (trial_days > 0) and (not is_active)

                # Admin check
                is_admin = user_id in self.admin_ids
                logger.info(f"Checking admin status for {user_id}: {is_admin} against {self.admin_ids}")

                from .database import get_support_link
                support_link = await get_support_link() or "https://t.me/SvoyVPN_support" # Fallback

                return web.json_response({
                    "user": {
                        "id": user_id,
                        "firstName": user_data.get('first_name', ''),
                        "lastName": user_data.get('last_name', ''),
                        "username": user_data.get('username', ''),
                        "photoUrl": photo_url_fetched,
                        "trialAvailable": trial_available,
                        "trialDays": trial_days,
                        "isAdmin": is_admin,
                        "supportLink": support_link
                    },
                    "subscription": {
                        "isActive": is_active,
                        "endDate": end_date_str,
                        "subscriptionUrl": subscription_url,
                        "token": user['subscription_token']
                    }
                })
                
        except Exception as e:
            logger.error(f"Error in api_get_user: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def api_activate_trial(self, request: web_request.Request) -> web.Response:
        """API: Активировать пробный период"""
        try:
            data = await request.json()
            init_data = data.get('initData', '')
            
            if not init_data:
                return web.json_response({"error": "initData required"}, status=400)
            
            if not self.bot:
                return web.json_response({"error": "Bot not initialized"}, status=500)
            
            bot_token = self.bot.token
            if not self.verify_telegram_webapp_data(init_data, bot_token):
                return web.json_response({"error": "Invalid initData"}, status=403)
            
            parsed_data = self.parse_telegram_init_data(init_data)
            user_str = parsed_data.get('user', '{}')
            user_data = json.loads(user_str) if user_str else {}
            user_id = int(user_data.get('id', 0))
            
            if not user_id:
                return web.json_response({"error": "User ID not found"}, status=400)
                
            async with get_connection() as conn:
                user_trial_used = await conn.fetchval("SELECT trial_used FROM users WHERE user_id = $1", user_id)
                if user_trial_used:
                    return web.json_response({"error": "Trial already used"}, status=400)
                
                trial_settings = await conn.fetchrow('SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1')
                trial_days = trial_settings['days'] if trial_settings else 0
                
                if trial_days <= 0:
                    return web.json_response({"error": "Trial not available"}, status=400)
                
                await conn.execute('''
                    UPDATE users SET 
                        trial_used = TRUE,
                        pay_subscribed = TRUE,
                        subscription_end = CASE 
                            WHEN subscription_end IS NULL OR subscription_end < CURRENT_DATE 
                            THEN CURRENT_DATE + ($1 || ' days')::INTERVAL
                            ELSE subscription_end + ($1 || ' days')::INTERVAL
                        END
                    WHERE user_id = $2
                ''', str(trial_days), user_id)
            
            return web.json_response({"status": "ok", "days": trial_days})
            
        except Exception as e:
            logger.error(f"Error in api_activate_trial: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)
            
    
    async def api_get_tariffs(self, request: web_request.Request) -> web.Response:
        """API: Получить список тарифов (обычные или со скидкой для продления)"""
        try:
            init_data = request.query.get('initData', '')
            user_id = 0

            if init_data and self.bot:
                try:
                    if self.verify_telegram_webapp_data(init_data, self.bot.token):
                        parsed_data = self.parse_telegram_init_data(init_data)
                        user_str = parsed_data.get('user', '{}')
                        user_data = json.loads(user_str) if user_str else {}
                        user_id = int(user_data.get('id', 0))
                except Exception:
                    pass  # fallback to normal prices
            
            from .plans import get_user_tariffs, get_subscription_plans
            
            # Получаем актуальные тарифы для пользователя, учитывая все скидки
            current_tariffs, is_renew, show_discount = await get_user_tariffs(user_id)
            
            # Предварительно загружаем базовые планы для расчета выгоды (зачеркнутых цен)
            regular_plans = await get_subscription_plans()
            
            tariffs = []
            
            # Получаем текущую базовую цену за 1 месяц (для расчета выгоды оптом)
            m1_reg = regular_plans.get('1_month', {})
            m1_rub = m1_reg.get('price_rub', 19900) / 100.0
            m1_stars = m1_reg.get('price_stars', 199)

            for plan_id, plan_data in current_tariffs.items():
                months = plan_data.get('duration', 1)
                price_rub = plan_data.get('price_rub', 0) / 100.0
                price_stars = plan_data.get('price_stars', 0)
                
                old_price = None
                old_price_stars = None
                
                base_id = plan_id.replace("_renew", "")
                base_plan = regular_plans.get(base_id)
                if base_plan:
                    base_rub = base_plan.get('price_rub', 0) / 100.0
                    base_stars = base_plan.get('price_stars', 0)
                    if price_rub < base_rub:
                        old_price = base_rub
                    if price_stars < base_stars:
                        old_price_stars = base_stars
                
                # Логика "Оптом дешевле" для всех планов
                if months > 1:
                    if not old_price and price_rub < (m1_rub * months):
                        old_price = m1_rub * months
                    if not old_price_stars and price_stars < (m1_stars * months):
                        old_price_stars = m1_stars * months
                
                tariffs.append({
                    "id": plan_id,
                    "months": months,
                    "price": price_rub,
                    "oldPrice": old_price,
                    "pricePerMonth": price_rub / months,
                    "priceStars": price_stars,
                    "oldPriceStars": old_price_stars,
                    "pricePerMonthStars": round(price_stars / months) if months > 0 else price_stars,
                    "popular": months == 12,
                    "isRenew": is_renew
                })
            
            return web.json_response(tariffs)
            
        except Exception as e:
            logger.error(f"Error in api_get_tariffs: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)
    
    async def api_get_payment_methods(self, request: web_request.Request) -> web.Response:
        """API: Получить способы оплаты"""
        try:
            from .plans import PAYMENT_METHODS
            
            methods = []
            for method_id, method_data in PAYMENT_METHODS.items():
                icon_map = {
                    "stars": "⭐",
                    "yookassa": "💳",
                    "cryptopay": "🪙"
                }
                methods.append({
                    "id": method_id,
                    "name": method_data.get('title', method_id),
                    "icon": icon_map.get(method_id, "💳"),
                    "description": method_data.get('description', ''),
                    "badge": method_data.get('badge', '')
                })
            
            return web.json_response(methods)
            
        except Exception as e:
            logger.error(f"Error in api_get_payment_methods: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)
    
    async def api_create_payment(self, request: web_request.Request) -> web.Response:
        """API: Создать платеж"""
        try:
            data = await request.json()
            init_data = data.get('initData', '')
            tariff_id = data.get('tariffId')
            payment_method = data.get('paymentMethod')
            device_count = data.get('deviceCount', 1)
            
            if not init_data or not tariff_id or not payment_method:
                return web.json_response({"error": "Missing required parameters"}, status=400)
            
            # Проверяем подлинность данных
            if not self.bot:
                return web.json_response({"error": "Bot not initialized"}, status=500)
            
            bot_token = self.bot.token
            if not self.verify_telegram_webapp_data(init_data, bot_token):
                return web.json_response({"error": "Invalid initData"}, status=403)
            
            # Парсим данные пользователя
            parsed_data = self.parse_telegram_init_data(init_data)
            user_str = parsed_data.get('user', '{}')
            user_data = json.loads(user_str) if user_str else {}
            user_id = int(user_data.get('id', 0))
            
            if not user_id:
                return web.json_response({"error": "User ID not found"}, status=400)
            
            # Получаем данные тарифа
            from .plans import get_user_tariffs
            current_tariffs, is_renew, _ = await get_user_tariffs(user_id)
            
            plan_data = current_tariffs.get(tariff_id)
            
            if not plan_data:
                logger.warning(f"Plan not found or not available: {tariff_id} for user {user_id}")
                return web.json_response({"error": "Tariff not found"}, status=404)
            
            # Получаем данные способа оплаты
            from .plans import PAYMENT_METHODS
            method_data = PAYMENT_METHODS.get(payment_method)
            
            if not method_data:
                return web.json_response({"error": "Payment method not found"}, status=404)
            
            # Вычисляем цену
            price_rub = plan_data.get('price_rub', 0)
            total_price = price_rub * device_count
            
            # Создаем платеж в зависимости от способа оплаты
            if payment_method == 'stars':
                # Оплата через Telegram Stars
                price_stars = plan_data.get('price_stars', 0) * device_count
                
                try:
                    # Создаем инвойс-ссылку для Stars
                    labeled_prices = [LabeledPrice(label=plan_data['title'], amount=price_stars)]
                    invoice_link = await self.bot.create_invoice_link(
                        title=f"VPN: {plan_data['title']}",
                        description=f"Подписка на {plan_data.get('duration', 1)} мес. ({device_count} устройство)",
                        payload=f"stars_{user_id}_{tariff_id}_{int(datetime.now().timestamp())}",
                        provider_token="", # Empty for Stars
                        currency="XTR",
                        prices=labeled_prices
                    )
                    return web.json_response({
                        "invoiceUrl": invoice_link,
                        "paymentId": f"stars_{user_id}_{int(datetime.now().timestamp())}"
                    })
                except Exception as e:
                    logger.error(f"Error creating Stars invoice: {e}", exc_info=True)
                    # Fallback to older method if invoice creation fails
                    bot_username = (await self.bot.get_me()).username
                    return web.json_response({
                        "invoiceUrl": f"https://t.me/{bot_username}?start=payment_{tariff_id}_{device_count}",
                        "paymentId": f"stars_{user_id}_{int(datetime.now().timestamp())}"
                    })

            elif payment_method == 'yookassa':
                # Оплата через ЮKassa
                if not self.yookassa_config or not self.yookassa_config.enabled:
                    return web.json_response({"error": "YooKassa not configured"}, status=500)
                
                total_amount_cents = plan_data.get('price_rub', 0) * device_count
                
                # Если есть provider_token, используем нативные инвойсы Telegram (они открываются внутри аппа)
                if self.yookassa_config.provider_token:
                    try:
                        labeled_prices = [LabeledPrice(label=plan_data['title'], amount=total_amount_cents)]
                        invoice_link = await self.bot.create_invoice_link(
                            title=f"VPN: {plan_data['title']}",
                            description=f"Подписка на {plan_data.get('duration', 1)} мес. ({device_count} устройство)",
                            payload=f"yoo_{user_id}_{tariff_id}_{int(datetime.now().timestamp())}",
                            provider_token=self.yookassa_config.provider_token,
                            currency="RUB",
                            prices=labeled_prices
                        )
                        return web.json_response({
                            "invoiceUrl": invoice_link,
                            "paymentId": f"tg_yoo_{user_id}_{int(datetime.now().timestamp())}"
                        })
                    except Exception as e:
                        logger.error(f"Error creating native YooKassa invoice: {e}", exc_info=True)
                        # Fallback to direct redirect if native fails
                
                # Стандартная оплата через ЮKassa Redirect (открывается в браузере)
                if not self.yookassa_client:
                    return web.json_response({"error": "YooKassa client not initialized"}, status=500)
                
                amount_rub = total_amount_cents / 100.0
                bot_username = (await self.bot.get_me()).username
                payment_data = self.yookassa_client.create_payment(
                    amount=amount_rub,
                    description=f"VPN подписка - {plan_data['title']}",
                    return_url=f"https://t.me/{bot_username}?start=payment_success",
                    metadata={
                        "user_id": user_id,
                        "plan_id": tariff_id,
                        "device_count": device_count
                    }
                )
                
                return web.json_response({
                    "paymentUrl": payment_data.get("confirmation_url"),
                    "paymentId": payment_data.get("id")
                })
            elif payment_method == "cryptopay":
                if not self.cryptopay_config or not self.cryptopay_config.enabled:
                    return web.json_response({"error": "Crypto Pay not configured"}, status=500)
                
                amount_rub = (plan_data.get('price_rub', 0) * device_count) / 100.0
                api_url = "https://testnet-pay.crypt.bot/api/createInvoice" if self.cryptopay_config.testnet else "https://pay.crypt.bot/api/createInvoice"
                payload_str = f"{user_id}:{tariff_id}:cryptopay:{device_count}"
                
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    headers = {"Crypto-Pay-API-Token": self.cryptopay_config.api_token}
                    data_pay = {
                        "currency_type": "fiat",
                        "fiat": "RUB",
                        "amount": f"{amount_rub:.2f}",
                        "description": f"VPN подписка - {plan_data['title']}",
                        "payload": payload_str
                    }
                    async with session.post(api_url, headers=headers, json=data_pay) as resp:
                        res = await resp.json()
                        if res.get("ok"):
                            # Можно так же отдавать 'bot_invoice_url', если хотим открывать в Telegram
                            invoice_url = res["result"].get("mini_app_invoice_url", res["result"]["bot_invoice_url"])
                            invoice_id = res["result"]["invoice_id"]
                            
                            # Сохраняем платеж в БД со статусом pending
                            try:
                                async with get_connection() as conn:
                                    # Определяем тип подписки (базовая проверка по id плана)
                                    subscription_plans = await get_subscription_plans()
                                    plan_type = "subscription" if tariff_id in subscription_plans else "renewal"
                                    
                                    await conn.execute('''
                                        INSERT INTO payments (user_id, amount, currency, plan_id, plan_type, status, yookassa_payment_id)
                                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                                    ''', 
                                        user_id, int(round(amount_rub * 100)), "RUB", 
                                        tariff_id, plan_type, "pending", str(invoice_id)
                                    )
                            except Exception as db_e:
                                logger.error(f"Error saving pending CryptoPay payment to DB: {db_e}")

                            # Отправляем уведомление в бот пользователю
                            try:
                                builder = InlineKeyboardBuilder()
                                builder.row(InlineKeyboardButton(text="💎 Перейти к оплате", url=invoice_url))
                                builder.row(InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_crypto:{invoice_id}"))
                                
                                await self.bot.send_message(
                                    user_id,
                                    f"🚀 <b>Счёт на оплату создан!</b>\n\n"
                                    f"Цена: <b>{amount_rub:.2f} ₽</b>\n"
                                    f"Тариф: <b>{plan_data['title']}</b>\n\n"
                                    f"Вы можете оплатить счёт в приложении Crypto Bot. После оплаты нажмите кнопку ниже для проверки.",
                                    parse_mode="HTML",
                                    reply_markup=builder.as_markup()
                                )
                            except Exception as msg_e:
                                logger.error(f"Error sending CryptoPay message to user {user_id}: {msg_e}")

                            return web.json_response({
                                "paymentUrl": invoice_url,
                                "paymentId": invoice_id
                            })
                        else:
                            logger.error(f"Crypto Pay API Error: {res}")
                            return web.json_response({"error": "Crypto Pay error"}, status=500)
            else:
                return web.json_response({"error": "Payment method not supported"}, status=400)
                
        except Exception as e:
            logger.error(f"Error in api_create_payment: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)
    
    async def api_get_servers(self, request: web_request.Request) -> web.Response:
        """API: Получить список серверов (публичная информация)"""
        try:
            async with get_connection() as conn:
                rows = await conn.fetch(
                    "SELECT id, name, ip, port, protocol, is_active, display_order, is_system FROM servers WHERE is_active = TRUE AND is_system = FALSE ORDER BY display_order ASC, id"
                )
                servers = []
                for r in rows:
                    emoji, cleaned_name = self.extract_emoji_and_name(r["name"])
                    servers.append({
                        "id": r["id"],
                        "name": cleaned_name,
                        "emoji": emoji,
                        "ip": r["ip"],
                        "port": r["port"],
                        "protocol": r["protocol"],
                    })
            logger.info(f"api_get_servers: found {len(servers)} active servers in DB")
            return web.json_response(servers)
        except Exception as e:
            logger.error(f"Error in api_get_servers: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def api_ping_server(self, request: web_request.Request) -> web.Response:
        """API: Пинг сервера по ID"""
        import asyncio
        server_id_str = request.query.get('id', '')
        if not server_id_str or not server_id_str.isdigit():
            return web.json_response({"error": "id required"}, status=400)
        
        server_id = int(server_id_str)
        
        try:
            async with get_connection() as conn:
                row = await conn.fetchrow(
                    "SELECT ip FROM servers WHERE id = $1 AND is_active = TRUE", server_id
                )
                if not row:
                    return web.json_response({"error": "Server not found"}, status=404)
                
                ip = row['ip']
            
            # ICMP ping с таймаутом 3 секунды
            try:
                proc = await asyncio.create_subprocess_exec(
                    'ping', '-c', '1', '-W', '3', ip,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                
                if proc.returncode == 0:
                    output = stdout.decode()
                    # Parse "time=12.3 ms" from ping output
                    import re
                    match = re.search(r'time[=<]([\d.]+)', output)
                    if match:
                        ping_ms = round(float(match.group(1)))
                        return web.json_response({"ping": ping_ms, "ip": ip})
                
                # Ping failed — try TCP connect as fallback
                try:
                    t0 = asyncio.get_event_loop().time()
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, 443), timeout=3
                    )
                    t1 = asyncio.get_event_loop().time()
                    writer.close()
                    await writer.wait_closed()
                    ping_ms = round((t1 - t0) * 1000)
                    return web.json_response({"ping": ping_ms, "ip": ip})
                except Exception:
                    return web.json_response({"ping": -1, "ip": ip})
                    
            except (asyncio.TimeoutError, Exception):
                return web.json_response({"ping": -1, "ip": ip})
                
        except Exception as e:
            logger.error(f"Error in api_ping_server: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def run(self, host: str = "0.0.0.0", port: int = 8080):
        """Запустить вебхук сервер"""
        # Настраиваем фильтр для логов
        bad_status_filter = BadStatusLineFilter()
        loggers_to_filter = [
            logging.getLogger('aiohttp'),
            logging.getLogger('aiohttp.web_protocol'),
            logging.getLogger('aiohttp.server'),
            logging.getLogger(),
        ]
        for log in loggers_to_filter:
            log.addFilter(bad_status_filter)
        
        logger.info(f"Starting webhook server on {host}:{port}")
        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            site = web.TCPSite(self.runner, host, port)
            await site.start()
            logger.info(f"✅ Webhook server started successfully on {host}:{port}")
            logger.info(f"✅ Available routes: /, /sub/{{token}}, /webhook/flyer, /webhook/yookassa")
        except Exception as e:
            logger.error(f"❌ Failed to start webhook server: {e}", exc_info=True)
            raise
    
    async def stop(self):
        """Остановить вебхук сервер"""
        logger.info("Stopping webhook server")
        if self.runner:
            try:
                await self.runner.cleanup()
                logger.info("Webhook server stopped")
            except Exception as e:
                logger.error(f"Error stopping webhook server: {e}")

    async def api_get_referral(self, request: web_request.Request) -> web.Response:
        """API: Получить реферальную информацию пользователя"""
        init_data = request.query.get('initData', '')
        if not init_data:
            return web.json_response({"error": "initData required"}, status=400)

        try:
            if not self.bot:
                return web.json_response({"error": "Bot not initialized"}, status=500)

            bot_token = self.bot.token
            if not self.verify_telegram_webapp_data(init_data, bot_token):
                return web.json_response({"error": "Invalid initData"}, status=403)

            parsed_data = self.parse_telegram_init_data(init_data)
            user_str = parsed_data.get('user', '{}')
            user_data = json.loads(user_str) if user_str else {}
            user_id = int(user_data.get('id', 0))
            if not user_id:
                return web.json_response({"error": "User ID not found"}, status=400)

            async with get_connection() as conn:
                row = await conn.fetchrow(
                    "SELECT referral_code, referral_count FROM users WHERE user_id = $1",
                    user_id
                )
                if not row:
                    return web.json_response({"error": "User not found"}, status=404)

                referral_code = row['referral_code'] or ''
                referral_count = row['referral_count'] or 0

                if not referral_code:
                    import secrets as _sec
                    referral_code = _sec.token_hex(4)
                    await conn.execute(
                        "UPDATE users SET referral_code = $1 WHERE user_id = $2",
                        referral_code, user_id
                    )

                ref_settings = await conn.fetchrow(
                    'SELECT inviter_bonus_days, invited_bonus_days FROM referral_settings ORDER BY id DESC LIMIT 1'
                )
                inviter_days = ref_settings['inviter_bonus_days'] if ref_settings else 5
                invited_days = ref_settings['invited_bonus_days'] if ref_settings else 3

            try:
                me = await self.bot.get_me()
                bot_username = me.username
            except Exception:
                bot_username = 'SvoyVPN_bot'

            ref_link = f"https://t.me/{bot_username}?start=ref_{referral_code}"

            return web.json_response({
                "referralCode": referral_code,
                "referralCount": referral_count,
                "inviterBonusDays": inviter_days,
                "invitedBonusDays": invited_days,
                "refLink": ref_link,
            })

        except Exception as e:
            logger.error(f"Error in api_get_referral: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def serve_news_image(self, request: web_request.Request) -> web.Response:
        path = request.match_info.get('path', '')
        if not path:
            return web.Response(text="Not found", status=404)
        
        path = path.lstrip('/')
        news_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'miniapp', 'news_images')
        file_path = os.path.normpath(os.path.join(news_dir, path))
        
        if not os.path.abspath(file_path).startswith(os.path.abspath(news_dir)):
            return web.Response(text="Forbidden", status=403)
            
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            guessed_type, _ = mimetypes.guess_type(file_path)
            content_type = guessed_type or "image/jpeg"
            return web.Response(body=content, content_type=content_type)
        except FileNotFoundError:
            return web.Response(text="Not found", status=404)

    async def api_get_news(self, request: web_request.Request) -> web.Response:
        try:
            async with get_connection() as conn:
                news = await conn.fetch("SELECT id, title, description, image_url FROM news ORDER BY created_at DESC")
                return web.json_response([dict(n) for n in news])
        except Exception as e:
            logger.error(f"Error getting news: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_add_news(self, request: web_request.Request) -> web.Response:
        try:
            reader = await request.multipart()
            
            init_data = None
            title = None
            description = None
            image_data = None
            image_filename = None
            
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == 'initData':
                    init_data = (await part.read(decode=True)).decode('utf-8')
                elif part.name == 'title':
                    title = (await part.read(decode=True)).decode('utf-8')
                elif part.name == 'description':
                    description = (await part.read(decode=True)).decode('utf-8')
                elif part.name == 'image':
                    image_filename = part.filename
                    image_data = await part.read(decode=True)

            if not init_data or not self.verify_telegram_webapp_data(init_data, self.bot.token):
                return web.json_response({"error": "Auth failed"}, status=403)
                
            parsed = self.parse_telegram_init_data(init_data)
            user_json = parsed.get('user', '{}')
            user_data = json.loads(user_json)
            user_id = int(user_data.get('id', 0))
            
            if user_id not in self.admin_ids:
                return web.json_response({"error": "Forbidden"}, status=403)
                
            image_url = None
            if image_data and image_filename:
                import uuid
                ext = os.path.splitext(image_filename)[1]
                if not ext: ext = '.jpg'
                new_filename = f"{uuid.uuid4()}{ext}"
                news_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'miniapp', 'news_images')
                os.makedirs(news_dir, exist_ok=True)
                with open(os.path.join(news_dir, new_filename), 'wb') as f:
                    f.write(image_data)
                image_url = f"/miniapp/news_images/{new_filename}"
            
            async with get_connection() as conn:
                await conn.execute(
                    "INSERT INTO news (title, description, image_url) VALUES ($1, $2, $3)",
                    title, description, image_url
                )
                
            return web.json_response({"status": "ok"})
        except Exception as e:
            logger.error(f"Error adding news: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def api_delete_news(self, request: web_request.Request) -> web.Response:
        try:
            data = await request.json()
            init_data = data.get('initData')
            news_id = data.get('newsId')
            
            if not init_data or not self.verify_telegram_webapp_data(init_data, self.bot.token):
                return web.json_response({"error": "Auth failed"}, status=403)
                
            parsed = self.parse_telegram_init_data(init_data)
            user_json = parsed.get('user', '{}')
            user_data = json.loads(user_json)
            user_id = int(user_data.get('id', 0))
            
            if user_id not in self.admin_ids:
                return web.json_response({"error": "Forbidden"}, status=403)
            
            async with get_connection() as conn:
                # Optionally delete image file from disk
                row = await conn.fetchrow("SELECT image_url FROM news WHERE id = $1", news_id)
                if row and row['image_url']:
                    try:
                        image_path = row['image_url'].replace('/miniapp/news_images/', '')
                        news_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'miniapp', 'news_images')
                        full_path = os.path.join(news_dir, image_path)
                        if os.path.exists(full_path):
                            os.remove(full_path)
                    except Exception as img_err:
                        logger.error(f"Error deleting news image file: {img_err}")

                await conn.execute("DELETE FROM news WHERE id = $1", news_id)
                
            return web.json_response({"status": "ok"})
        except Exception as e:
            logger.error(f"Error deleting news: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    # ═══ Android Auth Helpers ══════════════════════════════════════════════════

    # In-memory pending auth nonces: {nonce: {"user_id": int|None, "expires": datetime}}
    _auth_nonces: dict = {}
    # In-memory email OTP store: {email: {"code": str, "expires": datetime, "password_hash": str}}
    _email_otps: dict = {}
    # In-memory reset password store: {email: {"code": str, "expires": datetime}}
    _reset_otps: dict = {}
    JWT_SECRET = "svoyvpn_jwt_secret_change_in_production"
    JWT_ALGORITHM = "HS256"
    JWT_EXPIRY_DAYS = 365

    def _generate_jwt(self, user_id: int) -> str:
        payload = {
            "user_id": user_id,
            "exp": datetime.utcnow() + timedelta(days=self.JWT_EXPIRY_DAYS),
            "iat": datetime.utcnow(),
        }
        return pyjwt.encode(payload, self.JWT_SECRET, algorithm=self.JWT_ALGORITHM)

    def _verify_jwt(self, token: str) -> int | None:
        """Verify JWT and return user_id or None."""
        try:
            payload = pyjwt.decode(token, self.JWT_SECRET, algorithms=[self.JWT_ALGORITHM])
            return int(payload["user_id"])
        except Exception:
            return None

    def _get_jwt_user_id(self, request: web_request.Request) -> int | None:
        """Extract and verify Bearer JWT from Authorization header."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return self._verify_jwt(auth[7:])
        return None

    # ─── POST /api/auth/tg-init ───────────────────────────────────────────────

    async def api_auth_tg_init(self, request: web_request.Request) -> web.Response:
        """
        Android Telegram Login step 1: generate nonce, return bot deep link.
        The bot starts listening for /start auth_{nonce} from any user.
        """
        nonce = secrets.token_urlsafe(16)
        self._auth_nonces[nonce] = {
            "user_id": None,
            "expires": datetime.utcnow() + timedelta(minutes=10)
        }
        bot_username = os.getenv("BOT_USERNAME") or "SvoyVPN_robot"
        bot_url = f"https://t.me/{bot_username}?start=auth_{nonce}"
        logger.info(f"Telegram auth init: nonce={nonce[:8]}…")
        return web.json_response({"nonce": nonce, "botUrl": bot_url})

    # ─── GET /api/auth/tg-poll?nonce=X ────────────────────────────────────────

    async def api_auth_tg_poll(self, request: web_request.Request) -> web.Response:
        """
        Android Telegram Login step 2: poll until bot confirms identity.
        Returns JWT when confirmed, "pending" while waiting, "expired" on timeout.
        """
        nonce = request.query.get("nonce", "")
        if not nonce or nonce not in self._auth_nonces:
            return web.json_response({"status": "expired"})

        entry = self._auth_nonces[nonce]
        if datetime.utcnow() > entry["expires"]:
            del self._auth_nonces[nonce]
            return web.json_response({"status": "expired"})

        user_id = entry.get("user_id")
        if user_id is None:
            return web.json_response({"status": "pending"})

        # Confirmed — generate JWT and clean up
        token = self._generate_jwt(user_id)
        del self._auth_nonces[nonce]
        logger.info(f"Telegram auth confirmed: user_id={user_id}")
        return web.json_response({"status": "ok", "token": token})

    @classmethod
    def confirm_telegram_auth(cls, nonce: str, user_id: int):
        """Called by the bot handler when user sends /start auth_{nonce}."""
        if nonce in cls._auth_nonces:
            cls._auth_nonces[nonce]["user_id"] = user_id
            logger.info(f"Auth nonce {nonce[:8]}… confirmed for user_id={user_id}")

    # ─── POST /api/auth/email-otp ─────────────────────────────────────────────

    def _build_otp_html(self, code: str, email: str) -> str:
        """Build a branded HTML email body for OTP verification."""
        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Код подтверждения SvoyVPN</title>
</head>
<body style="margin:0;padding:0;background:#0f1923;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f1923;min-height:100vh;">
    <tr><td align="center" style="padding:40px 16px;">

      <table width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;">

        <!-- HEADER / LOGO -->
        <tr>
          <td align="center" style="padding-bottom:32px;">
            <table cellpadding="0" cellspacing="0">
              <tr>
                <td style="text-align:center;vertical-align:middle;">
                  <img src="https://xdoublegroup.online/miniapp/logo.png" width="64" height="64" alt="SvoyVPN Logo" style="display:block;border-radius:18px;outline:none;">
                </td>
                <td style="padding-left:14px;vertical-align:middle;">
                  <span style="font-size:24px;font-weight:700;color:#ffffff;letter-spacing:-0.4px;">SvoyVPN</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- CARD -->
        <tr>
          <td style="background:#18222d;border-radius:20px;padding:36px 32px;border:1px solid rgba(255,255,255,0.06);">

            <p style="margin:0 0 8px;font-size:22px;font-weight:700;color:#ffffff;text-align:center;">
              Подтвердите email
            </p>
            <p style="margin:0 0 32px;font-size:14px;color:#8e9db0;text-align:center;line-height:1.5;">
              Для завершения регистрации в&nbsp;SvoyVPN<br>введите этот код в&nbsp;приложении:
            </p>

            <!-- OTP CODE BOX -->
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td align="center" style="padding-bottom:32px;">
                  <div style="display:inline-block;background:#21303f;border:2px solid #3aa8fc;border-radius:16px;padding:20px 40px;">
                    <span style="font-size:40px;font-weight:800;color:#3aa8fc;letter-spacing:10px;font-family:'SF Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;">{code}</span>
                  </div>
                </td>
              </tr>
            </table>

            <!-- EXPIRY NOTICE -->
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td align="center" style="background:rgba(58,168,252,0.08);border-radius:10px;padding:12px 16px;margin-bottom:24px;">
                  <p style="margin:0;font-size:13px;color:#8e9db0;text-align:center;">
                    ⏱ Код действителен&nbsp;<strong style="color:#3aa8fc;">10&nbsp;минут</strong>
                  </p>
                </td>
              </tr>
            </table>

            <div style="height:24px;"></div>

            <!-- SECURITY NOTE -->
            <p style="margin:0;font-size:12px;color:#8e9db0;text-align:center;line-height:1.6;border-top:1px solid rgba(255,255,255,0.06);padding-top:24px;">
              Если вы не регистрировались в&nbsp;SvoyVPN&nbsp;— просто проигнорируйте это&nbsp;письмо.<br>
              Никому не&nbsp;сообщайте этот код.
            </p>

          </td>
        </tr>

        <!-- FOOTER -->
        <tr>
          <td align="center" style="padding-top:28px;">
            <p style="margin:0;font-size:12px;color:#4a5a6a;line-height:1.6;">
              © 2025 SvoyVPN &nbsp;·&nbsp;
              <a href="https://xdoublegroup.online" style="color:#3aa8fc;text-decoration:none;">xdoublegroup.online</a>
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    def _send_email(self, to_email: str, subject: str, html_body: str) -> None:
        """Send HTML email via SMTP (supports port 465 SSL and 587 STARTTLS)."""
        import smtplib
        import ssl
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        smtp_host = os.environ.get("SMTP_HOST", "")
        smtp_port = int(os.environ.get("SMTP_PORT", "465"))
        smtp_user = os.environ.get("SMTP_USER", "")
        smtp_pass = os.environ.get("SMTP_PASSWORD", "")
        smtp_from = os.environ.get("SMTP_FROM", smtp_user)
        if not smtp_host or not smtp_user:
            raise RuntimeError("SMTP not configured (set SMTP_HOST, SMTP_USER, SMTP_PASSWORD env vars)")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"SvoyVPN <{smtp_user}>"  # Standardize From to match SMTP user for better delivery
        msg["To"] = to_email
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=smtp_host.split('.')[-2] + '.' + smtp_host.split('.')[-1] if '.' in smtp_host else "svoyvpn.online")
        msg.attach(MIMEText(
            f"Ваш код подтверждения SvoyVPN.\nКод действителен 10 минут.\nНикому не сообщайте этот код.",
            "plain", "utf-8"
        ))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        ctx = ssl.create_default_context()
        if smtp_port == 465:
            # SSL from the start (smtplib.SMTP_SSL)
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=15) as s:
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_from, [to_email], msg.as_string())
        else:
            # STARTTLS (port 587 or any other)
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_from, [to_email], msg.as_string())

    async def api_auth_email_otp(self, request: web_request.Request) -> web.Response:
        """Send OTP code to email for registration verification."""
        try:
            data = await request.json()
            email = (data.get("email") or "").strip().lower()
            password = data.get("password") or ""

            if not email or "@" not in email:
                return web.json_response({"error": "Invalid email"}, status=400)
            if len(password) < 6:
                return web.json_response({"error": "Password too short"}, status=400)

            async with get_connection() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS app_accounts (
                        id SERIAL PRIMARY KEY,
                        email TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        user_id BIGINT REFERENCES users(user_id),
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                existing = await conn.fetchval("SELECT id FROM app_accounts WHERE email = $1", email)
                if existing:
                    return web.json_response({"error": "Email already registered"}, status=409)

            pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            code = str(secrets.randbelow(900000) + 100000)  # 6-digit code
            self._email_otps[email] = {
                "code": code,
                "expires": datetime.utcnow() + timedelta(minutes=10),
                "password_hash": pw_hash,
            }

            try:
                import asyncio
                loop = asyncio.get_event_loop()
                html_body = self._build_otp_html(code, email)
                await loop.run_in_executor(None, self._send_email, email,
                    "Ваш код подтверждения SvoyVPN",
                    html_body)
            except Exception as mail_err:
                logger.error(f"Failed to send OTP email to {email}: {mail_err}")
                return web.json_response({"error": "Не удалось отправить письмо. Проверьте email или попробуйте позже."}, status=500)

            logger.info(f"OTP sent to {email}")
            return web.json_response({"status": "sent"})

        except Exception as e:
            logger.error(f"Error in api_auth_email_otp: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def api_auth_reset_otp(self, request: web_request.Request) -> web.Response:
        """Send OTP code for password reset."""
        try:
            data = await request.json()
            email = (data.get("email") or "").strip().lower()
            if not email or "@" not in email:
                return web.json_response({"error": "Invalid email"}, status=400)

            async with get_connection() as conn:
                existing = await conn.fetchval("SELECT id FROM app_accounts WHERE email = $1", email)
                if not existing:
                    return web.json_response({"error": "Аккаунт с таким email не найден"}, status=404)

            code = str(secrets.randbelow(900000) + 100000)
            self._reset_otps[email] = {
                "code": code,
                "expires": datetime.utcnow() + timedelta(minutes=10)
            }

            try:
                import asyncio
                loop = asyncio.get_event_loop()
                html_body = self._build_otp_html(code, email)
                await loop.run_in_executor(None, self._send_email, email, "Восстановление пароля SvoyVPN", html_body)
            except Exception as mail_err:
                logger.error(f"Failed to send reset OTP to {email}: {mail_err}")
                return web.json_response({"error": "Не удалось отправить письмо"}, status=500)

            return web.json_response({"status": "sent"})
        except Exception as e:
            logger.error(f"Error in api_auth_reset_otp: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def api_auth_reset_password(self, request: web_request.Request) -> web.Response:
        """Reset password using OTP."""
        try:
            data = await request.json()
            email = (data.get("email") or "").strip().lower()
            otp = (data.get("otp") or "").strip()
            new_password = data.get("password") or ""

            if not email or not otp or len(new_password) < 6:
                return web.json_response({"error": "Некорректные данные"}, status=400)

            entry = self._reset_otps.get(email)
            if not entry or entry["code"] != otp or datetime.utcnow() > entry["expires"]:
                return web.json_response({"error": "Неверный или истекший код"}, status=400)

            pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
            async with get_connection() as conn:
                user_id = await conn.fetchval(
                    "UPDATE app_accounts SET password_hash = $1 WHERE email = $2 RETURNING user_id",
                    pw_hash, email
                )

            del self._reset_otps[email]
            token = self._generate_jwt(user_id)
            logger.info(f"Password reset for {email} → logged in")
            return web.json_response({"status": "ok", "token": token, "userId": user_id})
        except Exception as e:
            logger.error(f"Error in api_auth_reset_password: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    # ─── POST /api/auth/register ──────────────────────────────────────────────

    async def api_auth_register(self, request: web_request.Request) -> web.Response:
        """Email registration — verifies OTP, creates user + app_account, returns JWT."""
        try:
            data = await request.json()
            email = (data.get("email") or "").strip().lower()
            otp = (data.get("otp") or "").strip()

            if not email or not otp:
                return web.json_response({"error": "email and otp required"}, status=400)

            entry = self._email_otps.get(email)
            if not entry:
                return web.json_response({"error": "Код не найден. Запросите новый."}, status=400)
            if datetime.utcnow() > entry["expires"]:
                del self._email_otps[email]
                return web.json_response({"error": "Код истёк. Запросите новый."}, status=400)
            if entry["code"] != otp:
                return web.json_response({"error": "Неверный код"}, status=400)

            pw_hash = entry["password_hash"]
            del self._email_otps[email]  # consume OTP

            async with get_connection() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS app_accounts (
                        id SERIAL PRIMARY KEY,
                        email TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        user_id BIGINT REFERENCES users(user_id),
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)

                existing = await conn.fetchrow(
                    "SELECT id, user_id FROM app_accounts WHERE email = $1", email)
                if existing:
                    return web.json_response({"error": "Email already registered"}, status=409)

                import hashlib
                fake_user_id = -(abs(int(hashlib.md5(email.encode()).hexdigest(), 16)) % (10**15))
                while await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1", fake_user_id):
                    fake_user_id -= 1

                await conn.execute(
                    """INSERT INTO users (user_id, username, first_name, registration_date)
                       VALUES ($1, $2, $3, NOW())
                       ON CONFLICT (user_id) DO NOTHING""",
                    fake_user_id, email.split('@')[0], email.split('@')[0]
                )
                await conn.execute(
                    "INSERT INTO app_accounts (email, password_hash, user_id) VALUES ($1, $2, $3)",
                    email, pw_hash, fake_user_id
                )

            token = self._generate_jwt(fake_user_id)
            logger.info(f"Email registration confirmed: {email} → user_id={fake_user_id}")
            return web.json_response({"status": "ok", "token": token, "userId": fake_user_id})

        except Exception as e:
            logger.error(f"Error in api_auth_register: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    # ─── POST /api/auth/login ─────────────────────────────────────────────────

    async def api_auth_login(self, request: web_request.Request) -> web.Response:
        """Email login — verifies bcrypt hash and returns JWT."""
        try:
            data = await request.json()
            email = (data.get("email") or "").strip().lower()
            password = data.get("password") or ""

            async with get_connection() as conn:
                row = await conn.fetchrow(
                    "SELECT id, password_hash, user_id FROM app_accounts WHERE email = $1", email
                )
            if not row:
                return web.json_response({"error": "Invalid credentials"}, status=401)

            if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
                return web.json_response({"error": "Invalid credentials"}, status=401)

            user_id = row["user_id"]
            if not user_id:
                # Legacy account without user_id — create user row now
                import hashlib
                fake_user_id = -(abs(int(hashlib.md5(email.encode()).hexdigest(), 16)) % (10**15))
                async with get_connection() as conn:
                    while await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1", fake_user_id):
                        fake_user_id -= 1
                    await conn.execute(
                        """INSERT INTO users (user_id, username, first_name, registration_date)
                           VALUES ($1, $2, $3, NOW()) ON CONFLICT (user_id) DO NOTHING""",
                        fake_user_id, email.split('@')[0], email.split('@')[0]
                    )
                    await conn.execute(
                        "UPDATE app_accounts SET user_id = $1 WHERE email = $2",
                        fake_user_id, email
                    )
                user_id = fake_user_id

            token = self._generate_jwt(user_id)
            logger.info(f"Email login: {email} → user_id={user_id}")
            return web.json_response({"token": token, "userId": user_id})

        except Exception as e:
            logger.error(f"Error in api_auth_login: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    # ─── GET /api/user (JWT version for Android) ──────────────────────────────

    async def api_get_user_jwt(self, request: web_request.Request) -> web.Response:
        """
        GET /api/user with Bearer JWT — Android-app version of api_get_user.
        Returns the same JSON structure so the Android client reuses the same model.
        """
        user_id = self._get_jwt_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            async with get_connection() as conn:
                user = await conn.fetchrow(
                    "SELECT user_id, username, first_name, pay_subscribed, subscription_end, subscription_token, trial_used FROM users WHERE user_id = $1",
                    user_id
                )
                if not user:
                    return web.json_response({"error": "User not found"}, status=404)

                is_active = False
                end_date = None
                if user["pay_subscribed"] and user["subscription_end"]:
                    end_date = user["subscription_end"]
                    if hasattr(end_date, "date"):
                        end_date = end_date.date()
                    is_active = end_date >= datetime.now().date()

                from .subscriptions import get_user_subscription_url
                subscription_url = await get_user_subscription_url(user_id, None)

                end_date_str = end_date.isoformat() if end_date and hasattr(end_date, "isoformat") else None

                trial_settings = await conn.fetchrow("SELECT days FROM trial_settings ORDER BY id DESC LIMIT 1")
                trial_days = trial_settings["days"] if trial_settings else 0
                trial_available = (user["trial_used"] is False) and (trial_days > 0) and (not is_active)

                is_admin = user_id in self.admin_ids

                from .database import get_support_link
                support_link = await get_support_link() or "https://t.me/SvoyVPN_support"

                return web.json_response({
                    "user": {
                        "id": user_id,
                        "firstName": user.get("first_name") or "",
                        "lastName": "",
                        "username": user.get("username") or "",
                        "photoUrl": None,
                        "trialAvailable": trial_available,
                        "trialDays": trial_days,
                        "isAdmin": is_admin,
                        "supportLink": support_link,
                    },
                    "subscription": {
                        "isActive": is_active,
                        "endDate": end_date_str,
                        "subscriptionUrl": subscription_url,
                        "token": user["subscription_token"],
                    }
                })

        except Exception as e:
            logger.error(f"Error in api_get_user_jwt: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def run(self, host: str, port: int):
        """Запуск сервера"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        
    async def stop(self):
        """Остановка сервера"""
        if self.runner:
            await self.runner.cleanup()


