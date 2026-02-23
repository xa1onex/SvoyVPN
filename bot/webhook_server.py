"""
HTTP сервер для обработки вебхуков (YooKassa, Flyer) и subscription endpoint
"""
import json
import logging
from datetime import datetime
from typing import Optional
from aiohttp import web, web_request
from aiohttp.web_exceptions import HTTPBadRequest, HTTPNotFound, HTTPMethodNotAllowed
from aiohttp.http_exceptions import BadStatusLine, BadHttpMessage

from .config import FlyerConfig, YooKassaConfig
from .database import get_connection
from .subscriptions import create_or_activate_keys_for_all_servers

logger = logging.getLogger(__name__)


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
                
                # Получаем ключи (один на сервер благодаря уникальному индексу)
                keys = await conn.fetch('''
                    SELECT DISTINCT ON (server_id) vless_link, server_id
                    FROM vpn_keys
                    WHERE user_id = $1 
                      AND is_active = TRUE
                      AND (expires_at IS NULL OR DATE(expires_at) >= CURRENT_DATE)
                    ORDER BY server_id, id ASC
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
                
                # ✅ Если ключей нет, но подписка активна - создаём их автоматически
                if not keys and is_active:
                    logger.info(f"User {user_id} has no keys but subscription is active, creating...")
                    try:
                        # Создаём ключи вне транзакции (используем отдельное соединение)
                        from .subscriptions import create_or_activate_keys_for_all_servers
                        await create_or_activate_keys_for_all_servers(user_id)
                        # Повторно запрашиваем ключи
                        keys = await conn.fetch('''
                            SELECT DISTINCT ON (server_id) vless_link, server_id
                            FROM vpn_keys
                            WHERE user_id = $1 AND is_active = TRUE
                              AND (expires_at IS NULL OR DATE(expires_at) >= CURRENT_DATE)
                            ORDER BY server_id, id ASC
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
