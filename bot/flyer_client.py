"""
Клиент для работы с Flyer Service API
Документация: https://api.flyerservice.io/redoc
"""
import httpx
import logging
from typing import Dict, Any, Optional
from .config import FlyerConfig

logger = logging.getLogger(__name__)


class FlyerClient:
    """Клиент для работы с Flyer Service API"""
    
    def __init__(self, config: FlyerConfig):
        self.config = config
        self.api_url = config.api_url.rstrip("/")
        self.api_key = config.api_key
        self.enabled = config.enabled
        
        if not self.enabled:
            logger.warning("Flyer Service integration is disabled")
            return
            
        if not self.api_key:
            logger.warning("Flyer API key is not set")
            
        self._client = httpx.AsyncClient(
            base_url=self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
    
    async def close(self):
        """Закрыть HTTP клиент"""
        if hasattr(self, '_client'):
            await self._client.aclose()
    
    async def check_user(self, user_id: int, language_code: str = "ru") -> bool:
        """
        Проверить, прошёл ли пользователь обязательную подписку
        
        Args:
            user_id: ID пользователя Telegram
            language_code: Код языка (по умолчанию ru)
            
        Returns:
            True если пользователь прошёл подписку, False иначе
        """
        if not self.enabled or not self.api_key:
            return True  # Если сервис отключен, пропускаем проверку
        
        try:
            response = await self._client.post(
                "/api/check",
                json={
                    "user_id": user_id,
                    "language_code": language_code,
                }
            )
            response.raise_for_status()
            data = response.json()
            return data.get("subscribed", False)
        except Exception as e:
            logger.error(f"Error checking user {user_id} in Flyer Service: {e}")
            return True  # В случае ошибки пропускаем проверку
    
    async def create_task(
        self,
        user_id: int,
        channel_username: str,
        key_number: Optional[int] = None,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        Создать задание для пользователя
        
        Args:
            user_id: ID пользователя Telegram
            channel_username: Username канала (без @)
            key_number: Номер ключа в сервисе (опционально)
            **kwargs: Дополнительные параметры
            
        Returns:
            Данные созданного задания или None в случае ошибки
        """
        if not self.enabled or not self.api_key:
            logger.warning("Flyer Service is not enabled or API key is missing")
            return None
        
        try:
            payload = {
                "user_id": user_id,
                "channel_username": channel_username,
                **kwargs
            }
            
            if key_number:
                payload["key_number"] = key_number
            
            response = await self._client.post(
                "/api/tasks",
                json=payload
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error creating task in Flyer Service: {e}")
            return None
    
    async def get_task_status(self, task_id: int) -> Optional[Dict[str, Any]]:
        """
        Получить статус задания
        
        Args:
            task_id: ID задания
            
        Returns:
            Данные задания или None в случае ошибки
        """
        if not self.enabled or not self.api_key:
            return None
        
        try:
            response = await self._client.get(f"/api/tasks/{task_id}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting task status {task_id}: {e}")
            return None










