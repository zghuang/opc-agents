from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
    environment: str = "development"
    external_api_base_url: str = "http://localhost:8888"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()