"""
HTTP сервер для обработки вебхуков (YooKassa, Flyer) и subscription endpoint
"""
import json
import logging
import os
import mimetypes
import hashlib
import hmac
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, unquote
from aiohttp import web, web_request
from aiohttp.web_exceptions import HTTPBadRequest, HTTPNotFound, HTTPMethodNotAllowed
from aiohttp.http_exceptions import BadStatusLine, BadHttpMessage

from .config import FlyerConfig, YooKassaConfig
from .database import get_connection
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


class WebhookServer:
    """HTTP сервер для вебхуков и subscription endpoint"""
    
    def __init__(
        self,
        flyer_config: FlyerConfig,
        yookassa_config: Optional[YooKassaConfig] = None,
        bot_instance=None,
        yookassa_client=None,
        payment_processor=None
    ):
        self.flyer_config = flyer_config
        self.yookassa_config = yookassa_config
        self.bot = bot_instance
        self.yookassa_client = yookassa_client
        self.payment_processor = payment_processor
        self.app = web.Application()
        self.app.router.add_get('/', self.root_handler)
        self.app.router.add_get('/sub/{token}', self.handle_subscription)
        self.app.router.add_post('/webhook/flyer', self.handle_flyer_webhook)
        self.app.router.add_get('/webhook/flyer', self.health_check)
        if yookassa_config and yookassa_config.enabled:
            self.app.router.add_post('/webhook/yookassa', self.handle_yookassa_webhook)
        
        # Miniapp routes
        self.app.router.add_get('/miniapp', self.serve_miniapp)
        self.app.router.add_get('/miniapp/{path:.*}', self.serve_miniapp_static)
        
        # Miniapp API routes
        self.app.router.add_post('/api/user', self.api_get_user)
        self.app.router.add_get('/api/tariffs', self.api_get_tariffs)
        self.app.router.add_get('/api/payment-methods', self.api_get_payment_methods)
        self.app.router.add_post('/api/payment/create', self.api_create_payment)
        self.app.router.add_get('/api/servers', self.api_get_servers)
        
        self.app.middlewares.append(self.handle_bad_requests_middleware)
        self.runner = None
        logger.info(f"WebhookServer initialized with routes: /, /sub/{{token}}, /webhook/flyer, /webhook/yookassa")
    
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
                
                # Проверяем активность подписки
                is_active = await conn.fetchval('''
                    SELECT CASE
                        WHEN pay_subscribed = TRUE
                         AND subscription_end IS NOT NULL
                         AND DATE(subscription_end) >= CURRENT_DATE
                        THEN TRUE ELSE FALSE END
                    FROM users WHERE user_id = $1
                ''', user_id)
                
                # Получаем ключи только для активных серверов (один на сервер благодаря уникальному индексу)
                keys = await conn.fetch('''
                    SELECT DISTINCT ON (k.server_id) k.vless_link, k.server_id
                    FROM vpn_keys k
                    INNER JOIN servers s ON k.server_id = s.id
                    WHERE k.user_id = $1 
                      AND k.is_active = TRUE
                      AND s.is_active = TRUE
                      AND (k.expires_at IS NULL OR DATE(k.expires_at) >= CURRENT_DATE)
                    ORDER BY k.server_id, k.id ASC
                ''', user_id)
                
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
                            # Повторно запрашиваем ключи только для активных серверов
                            keys = await conn.fetch('''
                                SELECT DISTINCT ON (k.server_id) k.vless_link, k.server_id
                                FROM vpn_keys k
                                INNER JOIN servers s ON k.server_id = s.id
                                WHERE k.user_id = $1 
                                  AND k.is_active = TRUE
                                  AND s.is_active = TRUE
                                  AND (k.expires_at IS NULL OR DATE(k.expires_at) >= CURRENT_DATE)
                                ORDER BY k.server_id, k.id ASC
                            ''', user_id)
                        except Exception as e:
                            logger.error(f"Failed to auto-create keys for user {user_id}: {e}")
                
                # Формируем ответ
                body = "\n".join([k["vless_link"] for k in keys if k.get("vless_link")])
                
                logger.info(f"Returning subscription for user {user_id}: {len(keys)} keys, active={is_active}")
                
                headers = {
                    "Cache-Control": "no-store",
                    "Content-Disposition": 'attachment; filename="SvoyVPN"',
                    "profile-title": "SvoyVPN",
                    "announce": "SvoyVPN • Premium subscription active",
                    "subscription-userinfo": f"upload=0; download=0; total=0; expire={expire_ts}" if is_active else "Inactive"
                }
                
                return web.Response(
                    status=200,
                    text=body,
                    content_type="text/plain",
                    charset="utf-8",
                    headers=headers
                )
        except HTTPNotFound:
            # Пробрасываем HTTPNotFound дальше
            raise
        except Exception as e:
            logger.error(f"Error in handle_subscription: {e}", exc_info=True)
            raise HTTPNotFound()
    
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
                    "SELECT user_id, pay_subscribed, subscription_end, subscription_token FROM users WHERE user_id = $1",
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
                
                # --- Fetch accurate Photo from Telegram API --- #
                photo_url_fetched = user_data.get('photo_url', '')
                try:
                    # Attempt to fetch high-res photo if not provided by initData
                    if not photo_url_fetched and self.bot:
                        profile_photos = await self.bot.get_user_profile_photos(user_id, limit=1)
                        if profile_photos and profile_photos.photos:
                            first_photo_array = profile_photos.photos[0]
                            if first_photo_array:
                                best_photo = first_photo_array[-1]
                                file_info = await self.bot.get_file(best_photo.file_id)
                                photo_url_fetched = f"https://api.telegram.org/file/bot{self.bot.token}/{file_info.file_path}"
                except Exception as ex:
                    logger.warning(f"Could not fetch profile photo for user {user_id}: {ex}")

                return web.json_response({
                    "user": {
                        "id": user_id,
                        "firstName": user_data.get('first_name', ''),
                        "lastName": user_data.get('last_name', ''),
                        "username": user_data.get('username', ''),
                        "photoUrl": photo_url_fetched
                    },
                    "subscription": {
                        "isActive": is_active,
                        "endDate": end_date_str,
                        "subscriptionUrl": subscription_url
                    }
                })
                
        except Exception as e:
            logger.error(f"Error in api_get_user: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)
    
    async def api_get_tariffs(self, request: web_request.Request) -> web.Response:
        """API: Получить список тарифов"""
        try:
            from .plans import get_subscription_plans
            subscription_plans = await get_subscription_plans()
            tariffs = []
            
            for plan_id, plan_data in subscription_plans.items():
                months = plan_data.get('duration', 1)
                price_rub = plan_data.get('price_rub', 0) / 100.0  # Конвертируем из копеек
                
                # Вычисляем старую цену (для скидки)
                base_price_per_month = price_rub / months
                old_price = None
                if months > 1:
                    # Старая цена = цена за 1 месяц * количество месяцев
                    old_price = base_price_per_month * months * 1.2  # Примерная старая цена
                
                tariffs.append({
                    "id": plan_id,
                    "months": months,
                    "price": price_rub,
                    "oldPrice": old_price,
                    "pricePerMonth": price_rub / months,
                    "popular": months == 12  # Годовой тариф помечаем как популярный
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
                    "yookassa": "💳"
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
            from .plans import get_subscription_plans
            subscription_plans = await get_subscription_plans()
            plan_data = subscription_plans.get(tariff_id)
            
            if not plan_data:
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
                # Здесь должна быть логика создания invoice через Telegram Bot API
                # Пока возвращаем заглушку
                bot_username = (await self.bot.get_me()).username
                return web.json_response({
                    "invoiceUrl": f"https://t.me/{bot_username}?start=payment_{tariff_id}_{device_count}",
                    "paymentId": f"stars_{user_id}_{int(datetime.now().timestamp())}"
                })
            elif payment_method == 'yookassa':
                # Оплата через ЮKassa
                if not self.yookassa_client:
                    return web.json_response({"error": "YooKassa not configured"}, status=500)
                
                amount_rub = total_price / 100.0
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
            else:
                return web.json_response({"error": "Payment method not supported"}, status=400)
                
        except Exception as e:
            logger.error(f"Error in api_create_payment: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)
    
    async def api_get_servers(self, request: web_request.Request) -> web.Response:
        """API: Получить список серверов (публичная информация)"""
        try:
            async with get_connection() as conn:
                # Временно берем is_active для дебага
                rows = await conn.fetch(
                    "SELECT id, name, is_active FROM servers ORDER BY id"
                )
                servers = [{"id": r["id"], "name": f"{r['name']} ({r['is_active']})"} for r in rows]
            logger.info(f"api_get_servers returned {len(servers)} servers")
            if not servers:
                # Если серверов действительно 0, покажем это прямо в интерфейсе!
                servers = [{"id": -1, "name": "DEBUG: DB returned 0 rows"}]
            return web.json_response(servers)
        except Exception as e:
            logger.error(f"Error in api_get_servers: {e}", exc_info=True)
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
