"""Settings read from environment variables (or a .env file)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/seller_ops"

    # Fernet key used to encrypt each seller's Amazon refresh token at rest.
    # Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = ""

    # Amazon endpoints. They point to the local mock by default; in production they would be
    # https://api.amazon.com (LWA), https://sellingpartnerapi-eu.amazon.com and https://advertising-api-eu.amazon.com
    lwa_url: str = "http://localhost:8001"
    sp_api_url: str = "http://localhost:8001"
    ads_api_url: str = "http://localhost:8001"
    lwa_client_id: str = "demo-client-id"
    lwa_client_secret: str = "demo-client-secret"

    amazon_max_retries: int = 5
    amazon_max_concurrency: int = 4
    amazon_report_poll_seconds: float = 2.0

    # Exchange rates to EUR. Static defaults for the demo; production would load the ECB daily feed.
    fx_rates_to_eur: dict[str, float] = {"EUR": 1.0, "GBP": 1.17, "USD": 0.92}

    stock_window_days: int = 30
    safety_days: int = 7


@lru_cache
def get_settings() -> Settings:
    return Settings()
