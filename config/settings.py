"""
Configuration loader for the Melbourne Real Estate Intelligence Platform.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from config/.env or root .env
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / "config" / ".env")
load_dotenv(BASE_DIR / ".env")


class Settings:
    """Centralized application configuration settings."""

    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_NAME: str = os.getenv("DB_NAME", "melbourne_property_db")
    DB_USER: str = os.getenv("DB_USER", "postgres")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "postgres")

    @property
    def database_url(self) -> str:
        explicit_url = os.getenv("DATABASE_URL")
        if explicit_url:
            return explicit_url
        return (
            f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    ALERT_WEBHOOK_URL: str = os.getenv("ALERT_WEBHOOK_URL", "")
    ALERT_MAX_BUDGET: float = float(os.getenv("ALERT_MAX_BUDGET", "950000"))
    ALERT_MIN_DISCOUNT_PCT: float = float(os.getenv("ALERT_MIN_DISCOUNT_PCT", "5.0"))
    MAPBOX_ACCESS_TOKEN: str = os.getenv("MAPBOX_ACCESS_TOKEN", "")
    DOMAIN_API_KEY: str = os.getenv("DOMAIN_API_KEY", "")
    VGVR_SETTLED_SALES_CSV: Path = BASE_DIR / os.getenv(
        "VGVR_SETTLED_SALES_CSV", "data/vgvr_settled_sales.csv"
    )
    ACTIVE_LISTINGS_CSV: Path = BASE_DIR / os.getenv(
        "ACTIVE_LISTINGS_CSV", "data/active_listings.csv"
    )
    MODEL_ARTIFACT_PATH: Path = BASE_DIR / "config" / "avm_xgboost_model.json"


settings = Settings()

