from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 7
    environment: str = "development"
    external_api_base_url: str = "http://localhost:8888"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()