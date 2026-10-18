"""Application configuration via pydantic-settings."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenRouter
    openrouter_api_key: str = ""
    gen_model: str = "z-ai/glm-4.7-flash"
    fallback_model_1: str = "google/gemini-2.5-flash-lite"
    fallback_model_2: str = "deepseek/deepseek-v4.1-flash"

    # Cohere (optional)
    cohere_api_key: str = ""
    embed_provider: str = "local"   # "local" | "cohere"
    rerank_provider: str = "local"  # "local" | "cohere" | "off"

    # GitHub
    github_token: str = ""

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    # Paths
    data_dir: str = "./data"
    index_dir: str = "./data/index"

    # Rate limiting
    rate_limit_per_minute: int = 120
    max_concurrent_requests: int = 16

    # CORS
    cors_origins: str = "http://localhost:3000,http://localhost:8000,https://olake.io"

    # Admin
    admin_token: str = "change_me_before_production"

    # RAG Tuning
    bm25_top_k: int = 30
    vector_top_k: int = 30
    rerank_top_k: int = 6
    confidence_threshold: float = 0.30
    rerank: str = "on"
    enable_semantic_cache: bool = True
    cache_ttl_seconds: int = 3600  # 1h max for freshness SLA
    max_question_length: int = 1000
    max_context_tokens: int = 3000
    max_output_tokens: int = 500
    llm_temperature: float = 0.1

    # Freshness
    sync_interval_min: int = 30
    freshness_degraded_secs: int = 5400  # 90 min

    # Budget guard
    budget_degraded_threshold: float = 0.30
    budget_check_interval_secs: int = 300

    # Cohere quota guard (80% of trial cap → switch to local)
    cohere_quota_warn_pct: float = 0.80

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def index_path(self) -> str:
        return os.path.abspath(self.index_dir)

    @property
    def data_path(self) -> str:
        return os.path.abspath(self.data_dir)

    @property
    def rerank_enabled(self) -> bool:
        return self.rerank.lower() == "on"

    @property
    def fallback_models(self) -> list[str]:
        return [self.fallback_model_1, self.fallback_model_2]

    @property
    def use_cohere_embed(self) -> bool:
        return self.embed_provider.lower() == "cohere" and bool(self.cohere_api_key)

    @property
    def use_cohere_rerank(self) -> bool:
        return self.rerank_provider.lower() == "cohere" and bool(self.cohere_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
