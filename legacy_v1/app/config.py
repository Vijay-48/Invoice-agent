from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ap:ap@localhost:5432/ap"
    openai_api_key: str = ""              # read from .env; Agno only looks at real env vars
    openai_model: str = "gpt-4o"
    storage_dir: str = "./storage"
    ledger_dir: str = "./ledgers"        # Markdown day book / supplier / user ledgers
    company_gstin: str = ""

    auto_post_limit: float = 50000        # above this, always needs approval
    price_tolerance_pct: float = 2.0      # allowed unit-price variance vs PO
    amount_tolerance: float = 1.0         # rupees, for rounding differences
    min_confidence: float = 0.85          # extraction confidence floor
    max_pages: int = 5
    max_upload_mb: int = 15
    processing_timeout_minutes: int = 15  # PROCESSING longer than this = crashed worker

    gmail_credentials_file: str = "credentials.json"
    gmail_token_file: str = "token.json"
    gmail_query: str = "has:attachment -label:ap-processed"
    gmail_processed_label: str = "ap-processed"
    gmail_poll_seconds: int = 60


settings = Settings()
