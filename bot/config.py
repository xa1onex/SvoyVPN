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
    public_username: str | None = Field(
        None,
        description="Username бота без @ (для ссылок в подписке / Happ). Иначе BOT_PUBLIC_USERNAME из .env",
    )


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
    api_url: str = Field(default="https://api.flyerhubs.com", description="Flyer API URL")
    enabled: bool = Field(default=False, description="Enable Flyer Service integration")


class YooKassaConfig(BaseModel):
    shop_id: str | None = Field(None, description="YooKassa Shop ID")
    secret_key: str | None = Field(None, description="YooKassa Secret Key")
    provider_token: str | None = Field(None, description="YooKassa Provider Token for Telegram Invoices")
    enabled: bool = Field(default=False, description="Enable YooKassa integration")
    webhook_url: str | None = Field(None, description="Webhook URL for YooKassa notifications")


class CryptoPayConfig(BaseModel):
    api_token: str | None = Field(None, description="Crypto Pay API Token")
    enabled: bool = Field(default=False, description="Enable Crypto Pay integration")
    testnet: bool = Field(default=False, description="Use Crypto Pay Testnet")


class RemnawaveConfig(BaseModel):
    enabled: bool = Field(default=False, description="Enable Remnawave panel integration")
    panel_url: str | None = Field(None, description="Remnawave panel base URL")
    api_token: str | None = Field(None, description="Remnawave API token (from panel Settings → API)")
    config_profile_uuid: str = Field(
        default="00000000-0000-0000-0000-000000000000",
        description="Default Config Profile UUID",
    )
    inbound_uuid: str = Field(
        default="b1ac2590-d0c3-4e58-bb62-4aae2280f69e",
        description="Inbound UUID inside the config profile (e.g. VLESS-gRPC)",
    )
    internal_squad_uuid: str = Field(
        default="b5f0d64c-ec52-4bd6-87c8-98495d63209c",
        description="Internal Squad UUID assigned to bot users",
    )
    grpc_path: str = Field(default="grpc.remnawave", description="gRPC serviceName for hosts")


class AppConfig(BaseModel):
    bot: BotConfig
    xui: XUIConfig
    remnawave: RemnawaveConfig = Field(default_factory=RemnawaveConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    payment: PaymentConfig = Field(default_factory=PaymentConfig)
    flyer: FlyerConfig = Field(default_factory=FlyerConfig)
    yookassa: YooKassaConfig = Field(default_factory=YooKassaConfig)
    cryptopay: CryptoPayConfig = Field(default_factory=CryptoPayConfig)
    subscription_base_url: str | None = Field(None, description="Base URL for subscription links")
    happ_open_base_urls: list[str] = Field(
        default_factory=list,
        description="Open domains for /happy-link/ (anti-block, fallback)",
    )
    app_url: str | None = Field(None, description="Base URL for miniapp (APP_URL)")


def load_config() -> AppConfig:
    """Load configuration from environment (.env)."""
    load_dotenv()

    _pub = os.getenv("BOT_PUBLIC_USERNAME", "").strip().lstrip("@") or None
    bot = BotConfig(
        bot_token=os.environ["BOT_TOKEN"],
        admin_ids=[int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()],
        public_username=_pub,
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
        api_url=os.getenv("FLYER_API_URL", "https://api.flyerhubs.com"),
        enabled=os.getenv("FLYER_ENABLED", "false").lower() == "true",
    )

    yookassa = YooKassaConfig(
        shop_id=os.getenv("YOOKASSA_SHOP_ID"),
        secret_key=os.getenv("YOOKASSA_SECRET_KEY"),
        provider_token=os.getenv("YOOKASSA_PROVIDER_TOKEN"),
        enabled=os.getenv("YOOKASSA_ENABLED", "false").lower() == "true",
        webhook_url=os.getenv("YOOKASSA_WEBHOOK_URL"),
    )
    
    api_token = os.getenv("CRYPTOPAY_API_TOKEN")
    if api_token:
        api_token = api_token.strip().strip('"').strip("'")
        
    cryptopay = CryptoPayConfig(
        api_token=api_token,
        enabled=os.getenv("CRYPTOPAY_ENABLED", "false").lower() == "true",
        testnet=os.getenv("CRYPTOPAY_TESTNET", "false").lower() == "true",
    )
    
    subscription_base_url = (
        os.getenv("SUBSCRIPTION_BASE_URL") or 
        os.getenv("PUBLIC_BASE_URL") or 
        os.getenv("WEBHOOK_BASE_URL") or 
        None
    )
    if subscription_base_url:
        subscription_base_url = subscription_base_url.rstrip("/")

    from .happ_link import parse_happ_open_base_urls

    happ_open_base_urls = parse_happ_open_base_urls()
    
    app_url = os.getenv("APP_URL")
    if app_url:
        app_url = app_url.rstrip("/")

    remnawave = RemnawaveConfig(
        enabled=os.getenv("REMNAWAVE_ENABLED", "false").lower() == "true",
        panel_url=(os.getenv("REMNAWAVE_PANEL_URL") or "").rstrip("/") or None,
        api_token=os.getenv("REMNAWAVE_API_TOKEN"),
        config_profile_uuid=os.getenv(
            "REMNAWAVE_CONFIG_PROFILE_UUID",
            "00000000-0000-0000-0000-000000000000",
        ),
        inbound_uuid=os.getenv(
            "REMNAWAVE_INBOUND_UUID",
            "b1ac2590-d0c3-4e58-bb62-4aae2280f69e",
        ),
        internal_squad_uuid=os.getenv(
            "REMNAWAVE_INTERNAL_SQUAD_UUID",
            "b5f0d64c-ec52-4bd6-87c8-98495d63209c",
        ),
        grpc_path=os.getenv("REMNAWAVE_GRPC_PATH", "grpc.remnawave"),
    )

    return AppConfig(
        bot=bot,
        xui=xui,
        remnawave=remnawave,
        database=database,
        payment=payment,
        flyer=flyer,
        yookassa=yookassa,
        cryptopay=cryptopay,
        subscription_base_url=subscription_base_url,
        happ_open_base_urls=happ_open_base_urls,
        app_url=app_url,
    )

