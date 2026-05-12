"""
Клиент для работы с API ЮKassa
Документация: https://yookassa.ru/developers/api
"""
import logging
from typing import Optional, Dict, Any
from yookassa import Configuration, Payment
from yookassa.domain.notification import WebhookNotification
from .config import YooKassaConfig

logger = logging.getLogger(__name__)


def _metadata_string_values(metadata: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """ЮKassa: значения metadata — строки UTF-8 (до 512 символов)."""
    if not metadata:
        return {}
    out: Dict[str, str] = {}
    for k, v in metadata.items():
        key = str(k)[:32]
        if v is None:
            out[key] = ""
        else:
            s = str(v)
            out[key] = s if len(s) <= 512 else s[:512]
    return out


class YooKassaClient:
    """Клиент для работы с API ЮKassa"""
    
    def __init__(self, config: YooKassaConfig):
        self.config = config
        if config.enabled and config.shop_id and config.secret_key:
            Configuration.account_id = config.shop_id
            Configuration.secret_key = config.secret_key
        else:
            logger.warning("YooKassa is not properly configured")
    
    def create_payment(
        self,
        amount: float,
        description: str,
        return_url: str,
        metadata: Optional[Dict[str, Any]] = None,
        save_payment_method: bool = False,
        idempotency_key: Optional[str] = None,
        merchant_customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Создать платеж через ЮKassa
        
        Args:
            amount: Сумма платежа в рублях
            description: Описание платежа
            return_url: URL для возврата после оплаты
            metadata: Дополнительные метаданные (например, user_id, plan_id)
        
        Returns:
            Словарь с данными платежа, включая confirmation_url
        """
        if not self.config.enabled:
            raise RuntimeError("YooKassa is not enabled")

        meta = _metadata_string_values(metadata)
        payment_dict: Dict[str, Any] = {
            "amount": {
                "value": f"{amount:.2f}",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": return_url
            },
            "description": description,
            "capture": True,
            "metadata": meta,
        }
        if save_payment_method:
            payment_dict["save_payment_method"] = True
            mc = merchant_customer_id or meta.get("user_id")
            if mc:
                payment_dict["merchant_customer_id"] = str(mc)[:200]
            logger.info(
                "YooKassa payment with save_payment_method (привязка для автоплатежей)"
            )

        try:
            payment = Payment.create(payment_dict, idempotency_key=idempotency_key)
            logger.info(f"YooKassa payment created: {payment.id}")
            return {
                "id": payment.id,
                "status": payment.status,
                "confirmation_url": payment.confirmation.confirmation_url if payment.confirmation else None,
                "amount": payment.amount.value,
                "currency": payment.amount.currency
            }
        except Exception as e:
            logger.error(f"Error creating YooKassa payment: {e}", exc_info=True)
            raise

    def create_recurring_payment(
        self,
        amount: float,
        description: str,
        payment_method_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Автоплатёж по сохранённому способу оплаты (без redirect).
        https://yookassa.ru/developers/payments/recurring-payments
        """
        if not self.config.enabled:
            raise RuntimeError("YooKassa is not enabled")

        payment_dict: Dict[str, Any] = {
            "amount": {
                "value": f"{amount:.2f}",
                "currency": "RUB",
            },
            "capture": True,
            "description": description,
            "metadata": _metadata_string_values(metadata),
            "payment_method_id": payment_method_id,
        }
        try:
            payment = Payment.create(payment_dict, idempotency_key=idempotency_key)
            logger.info(f"YooKassa recurring payment created: {payment.id}")
            return {
                "id": payment.id,
                "status": payment.status,
                "confirmation_url": payment.confirmation.confirmation_url if payment.confirmation else None,
                "amount": payment.amount.value,
                "currency": payment.amount.currency,
            }
        except Exception as e:
            logger.error(f"Error creating YooKassa recurring payment: {e}", exc_info=True)
            raise

    def get_payment_status(self, payment_id: str) -> Dict[str, Any]:
        """
        Получить статус платежа
        
        Args:
            payment_id: ID платежа в ЮKassa
        
        Returns:
            Словарь со статусом платежа
        """
        if not self.config.enabled:
            raise RuntimeError("YooKassa is not enabled")
        
        try:
            payment = Payment.find_one(payment_id)
            return {
                "id": payment.id,
                "status": payment.status,
                "paid": payment.paid,
                "amount": payment.amount.value if payment.amount else None,
                "currency": payment.amount.currency if payment.amount else None,
                "metadata": payment.metadata or {}
            }
        except Exception as e:
            logger.error(f"Error getting YooKassa payment status: {e}", exc_info=True)
            raise
    
    def parse_webhook(self, request_body: dict, request_headers: dict) -> Optional[Dict[str, Any]]:
        """
        Парсинг webhook уведомления от ЮKassa
        
        Args:
            request_body: Тело запроса (JSON)
            request_headers: Заголовки запроса
        
        Returns:
            Словарь с данными уведомления или None если ошибка
        """
        try:
            notification = WebhookNotification(request_body)
            payment_object = notification.object
            
            return {
                "event": notification.event,
                "payment_id": payment_object.id,
                "status": payment_object.status,
                "paid": payment_object.paid,
                "amount": payment_object.amount.value if payment_object.amount else None,
                "currency": payment_object.amount.currency if payment_object.amount else None,
                "metadata": payment_object.metadata or {}
            }
        except Exception as e:
            logger.error(f"Error parsing YooKassa webhook: {e}", exc_info=True)
            return None










