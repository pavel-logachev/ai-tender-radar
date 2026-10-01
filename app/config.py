from pydantic_settings import BaseSettings


DEFAULT_ROUTERAI_MODEL = "qwen/qwen3.7-plus"
ROUTERAI_FALLBACK_MODEL = "deepseek/deepseek-v4-pro"


class Settings(BaseSettings):
    database_url: str
    app_env: str = "dev"

    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: str | None = None
    telegram_proxy_url: str | None = None
    telegram_llm_generation_enabled: bool = True

    email_smtp_host: str | None = None
    email_smtp_port: int = 587
    email_smtp_user: str | None = None
    email_smtp_password: str | None = None
    email_from: str | None = None
    email_to: str | None = None

    z360_base_url: str = "https://api.zakupki360.ru"
    z360_login: str | None = None
    z360_password: str | None = None
    z360_rate_limit_seconds: float = 1.1
    z360_document_rate_limit_seconds: float | None = None

    gigachat_auth_key: str | None = None
    gigachat_scope: str = "GIGACHAT_API_B2B"
    gigachat_oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_api_base_url: str = "https://gigachat.devices.sberbank.ru/api"
    gigachat_model: str = "GigaChat-2-Pro"
    gigachat_verify_ssl: bool = False
    gigachat_timeout_seconds: int = 180

    llm_provider: str = "gigachat"
    llm_model: str | None = None
    llm_json_mode: bool = False

    routerai_api_key: str | None = None
    routerai_base_url: str = "https://routerai.ru/api/v1"
    routerai_model: str = DEFAULT_ROUTERAI_MODEL
    routerai_fallback_model: str = ROUTERAI_FALLBACK_MODEL
    routerai_timeout_seconds: int = 180
    routerai_connect_timeout_seconds: int = 30
    routerai_read_timeout_seconds: int = 900
    routerai_write_timeout_seconds: int = 30
    routerai_pool_timeout_seconds: int = 30
    routerai_thinking_enabled: bool = False
    routerai_reasoning_effort: str | None = None


settings = Settings()
