import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv


class XUIConfig(BaseModel):
    base_url: str = Field(..., description="Base URL of x-ui/3x-ui panel, e.g. https://your-host:54321")
    username: str | None = Field(None, description="Panel username (if using session-based login)")
    password: str | None = Field(None, description="Panel password (if using session-based login)")
    api_token: str | None = Field(None, description="Bearer token, if your panel uses token-based auth")


class BotConfig(BaseModel):
    bot_token: str
    admin_ids: list[int] = Field(default_factory=list)


class DatabaseConfig(BaseModel):
    db_url: str | None = Field(None, description="PostgreSQL database URL (DATABASE_URL)")
    db_host: str = Field(default="localhost", description="PostgreSQL host")
    db_port: str = Field(default="5432", description="PostgreSQL port")
    db_name: str = Field(default="vpn_db", description="PostgreSQL database name")
    db_user: str = Field(default="postgres", description="PostgreSQL user")
    db_password: str = Field(default="", description="PostgreSQL password")


class PaymentConfig(BaseModel):
    min_payment: float = Field(default=100.0, description="Minimum payment amount")


class FlyerConfig(BaseModel):
    api_key: str | None = Field(None, description="Flyer Service API key")
    api_url: str = Field(default="https://api.flyerservice.io", description="Flyer Service API URL")
    enabled: bool = Field(default=False, description="Enable Flyer Service integration")


class YooKassaConfig(BaseModel):
    shop_id: str | None = Field(None, description="YooKassa Shop ID")
    secret_key: str | None = Field(None, description="YooKassa Secret Key")
    provider_token: str | None = Field(None, description="YooKassa Provider Token for Telegram Invoices")
    enabled: bool = Field(default=False, description="Enable YooKassa integration")
    webhook_url: str | None = Field(None, description="Webhook URL for YooKassa notifications")


class AppConfig(BaseModel):
    bot: BotConfig
    xui: XUIConfig
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    payment: PaymentConfig = Field(default_factory=PaymentConfig)
    flyer: FlyerConfig = Field(default_factory=FlyerConfig)
    yookassa: YooKassaConfig = Field(default_factory=YooKassaConfig)
    subscription_base_url: str | None = Field(None, description="Base URL for subscription links")
    app_url: str | None = Field(None, description="Base URL for miniapp (APP_URL)")


def load_config() -> AppConfig:
    """Load configuration from environment (.env)."""
    load_dotenv()

    bot = BotConfig(
        bot_token=os.environ["BOT_TOKEN"],
        admin_ids=[int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()],
    )

    xui = XUIConfig(
        base_url=os.environ["XUI_BASE_URL"].rstrip("/"),
        username=os.getenv("XUI_USERNAME"),
        password=os.getenv("XUI_PASSWORD"),
        api_token=os.getenv("XUI_API_TOKEN"),
    )

    database = DatabaseConfig(
        db_url=os.getenv("DATABASE_URL"),
        db_host=os.getenv("DB_HOST", "localhost"),
        db_port=os.getenv("DB_PORT", "5432"),
        db_name=os.getenv("DB_NAME", "vpn_db"),
        db_user=os.getenv("DB_USER", "postgres"),
        db_password=os.getenv("DB_PASSWORD", ""),
    )

    payment = PaymentConfig(
        min_payment=float(os.getenv("MIN_PAYMENT", "100.0")),
    )

    flyer = FlyerConfig(
        api_key=os.getenv("FLYER_API_KEY"),
        api_url=os.getenv("FLYER_API_URL", "https://api.flyerservice.io"),
        enabled=os.getenv("FLYER_ENABLED", "false").lower() == "true",
    )

    yookassa = YooKassaConfig(
        shop_id=os.getenv("YOOKASSA_SHOP_ID"),
        secret_key=os.getenv("YOOKASSA_SECRET_KEY"),
        provider_token=os.getenv("YOOKASSA_PROVIDER_TOKEN"),
        enabled=os.getenv("YOOKASSA_ENABLED", "false").lower() == "true",
        webhook_url=os.getenv("YOOKASSA_WEBHOOK_URL"),
    )
    
    subscription_base_url = (
        os.getenv("SUBSCRIPTION_BASE_URL") or 
        os.getenv("PUBLIC_BASE_URL") or 
        os.getenv("WEBHOOK_BASE_URL") or 
        None
    )
    if subscription_base_url:
        subscription_base_url = subscription_base_url.rstrip("/")
    
    app_url = os.getenv("APP_URL")
    if app_url:
        app_url = app_url.rstrip("/")

    return AppConfig(
        bot=bot,
        xui=xui,
        database=database,
        payment=payment,
        flyer=flyer,
        yookassa=yookassa,
        subscription_base_url=subscription_base_url,
        app_url=app_url,
    )

