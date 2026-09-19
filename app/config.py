from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://autopilot:autopilot@localhost:5432/autopilot"
    redis_url: str = "redis://localhost:6379/0"
    routing_config_path: str = str(ROOT / "config" / "routing.yaml")
    default_model: str = "gpt-4o-mini"
    request_timeout_s: float = 60.0
    mock_providers: bool = False
    admin_token: str = "admin-dev-token"
    alert_webhook_url: str = ""
    soft_cap_ratio: float = 0.8
    cache_enabled: bool = True
    cache_ttl_s: int = 3600


class RoutingConfig(dict):
    @property
    def prices_per_1m(self) -> dict[str, list[float]]:
        return self["prices_per_1m"]

    @property
    def tiers(self) -> dict[str, dict]:
        return self["tiers"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_routing_config() -> RoutingConfig:
    with open(get_settings().routing_config_path) as f:
        return RoutingConfig(yaml.safe_load(f))
