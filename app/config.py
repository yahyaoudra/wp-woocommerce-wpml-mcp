from __future__ import annotations

from pathlib import Path
from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    wp_url: HttpUrl
    wc_consumer_key: str = Field(min_length=8)
    wc_consumer_secret: str = Field(min_length=8)
    wp_username: str | None = None
    wp_app_password: str | None = None

    mcp_host: str = "0.0.0.0"
    mcp_port: int = Field(default=8090, ge=1, le=65535)
    mcp_path: str = "/mcp"
    http_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    verify_tls: bool = True
    log_level: str = "INFO"
    audit_log: Path = Path("/data/audit.jsonl")

    allow_writes: bool = False
    allow_price_writes: bool = False
    allow_stock_writes: bool = False
    allow_publish: bool = False
    allow_external_image_urls: bool = False
    max_bulk_write: int = Field(default=20, ge=1, le=100)

    translation_copy_price: bool = True
    translation_copy_images: bool = True
    translation_copy_physical_fields: bool = True
    translation_copy_stock: bool = False

    allowed_meta_keys: str = "rank_math_title,rank_math_description,_yoast_wpseo_title,_yoast_wpseo_metadesc,lifestyle-gallery"

    @field_validator("mcp_path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        if not v.startswith("/"):
            v = "/" + v
        return v.rstrip("/") or "/mcp"

    @property
    def base_url(self) -> str:
        return str(self.wp_url).rstrip("/")

    @property
    def meta_key_allowlist(self) -> set[str]:
        keys = {x.strip() for x in self.allowed_meta_keys.split(",") if x.strip()}
        # Required by the WearInstinct product form custom gallery field.
        keys.add("lifestyle-gallery")
        return keys


def get_settings() -> Settings:
    return Settings()
