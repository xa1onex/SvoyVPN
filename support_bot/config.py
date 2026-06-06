import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv


class SupportBotConfig(BaseModel):
    bot_token: str
    main_bot_token: str
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    staff_ids: list[int] = Field(default_factory=list)
    main_bot_admin_ids: list[int] = Field(default_factory=list)
    service_name: str = "SvoyVPN"
    journal_unit: str = "svoyvpn"
    journal_lines: int = 80


def load_support_config() -> SupportBotConfig:
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
    load_dotenv()

    staff_raw = os.getenv("SUPPORT_STAFF_IDS") or os.getenv("ADMIN_IDS", "")
    admin_raw = os.getenv("ADMIN_IDS", "")

    token = os.getenv("SUPPORT_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SUPPORT_BOT_TOKEN is required in .env")

    main_token = os.getenv("BOT_TOKEN", "").strip()
    if not main_token:
        raise RuntimeError("BOT_TOKEN is required for user notifications")

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required in .env")

    return SupportBotConfig(
        bot_token=token,
        main_bot_token=main_token,
        openai_api_key=api_key,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        staff_ids=[int(x) for x in staff_raw.split(",") if x.strip()],
        main_bot_admin_ids=[int(x) for x in admin_raw.split(",") if x.strip()],
        service_name=os.getenv("SUPPORT_SERVICE_NAME", "SvoyVPN"),
        journal_unit=os.getenv("SUPPORT_JOURNAL_UNIT", "svoyvpn"),
        journal_lines=int(os.getenv("SUPPORT_JOURNAL_LINES", "80")),
    )


def is_staff(user_id: int, config: SupportBotConfig) -> bool:
    ids = set(config.staff_ids) | set(config.main_bot_admin_ids)
    return user_id in ids
