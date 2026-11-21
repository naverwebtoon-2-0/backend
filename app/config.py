from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    revision: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://nw_app:nw_app@127.0.0.1:5432/naverwebtoon"
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    public_base_url: str = "http://127.0.0.1:8080"

    # 인증
    jwt_secret: str = "change-me-in-production-please-32b"  # noqa: S105 (운영에서는 검증으로 거부)
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_days: int = 30
    password_min_length: int = 8
    author_invite_codes: Annotated[list[str], NoDecode] = Field(default_factory=list)
    login_max_attempts: int = 10
    login_window_seconds: int = 300

    # 외부 연동 전환
    ai_backend: Literal["fake", "clova"] = "fake"
    storage_backend: Literal["fake", "s3"] = "fake"

    # Object Storage
    storage_endpoint: str = "https://kr.object.ncloudstorage.com"
    storage_region: str = "kr-standard"
    storage_bucket: str = "naverwebtoon"
    storage_access_key: str = ""
    storage_secret_key: str = ""
    fake_storage_dir: str = ".fake-storage"
    upload_url_ttl_seconds: int = 600
    image_url_ttl_seconds: int = 3600
    audio_url_ttl_seconds: int = 6 * 3600

    # 업로드 제한
    max_image_bytes: int = 20 * 1024 * 1024
    max_images_per_episode: int = 100

    # CLOVA
    clova_ocr_invoke_url: str = ""
    clova_ocr_secret: str = ""
    clova_studio_url: str = "https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-005"
    clova_studio_api_key: str = ""
    # 음성 합성: CLOVA Voice Premium (CLOVA Dubbing은 실시간 API 미제공)
    clova_voice_url: str = "https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts"
    clova_voice_client_id: str = ""
    clova_voice_client_secret: str = ""
    # 효과음 프리셋 음원 위치 (Object Storage, {prefix}/{tag}.wav). 없으면 내장 생성기 사용
    sfx_preset_prefix: str = "sfx/presets"
    ai_timeout_seconds: float = 30.0
    ai_max_retries: int = 3

    # 판정·쿼터
    low_ocr_confidence_threshold: float = 0.8
    quota_dubbing_chars: int = 100_000
    quota_ocr_images: int = 500

    # 워커
    worker_poll_seconds: float = 1.0
    scheduler_interval_seconds: float = 30.0
    job_heartbeat_timeout_seconds: int = 120
    job_max_attempts: int = 3

    @field_validator("cors_allowed_origins", "author_invite_codes", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @model_validator(mode="after")
    def _production_guard(self) -> "Settings":
        if self.app_env == "production" and (
            self.jwt_secret.startswith("change-me") or len(self.jwt_secret) < 32
        ):
            raise ValueError("운영 환경에서는 32자 이상의 JWT_SECRET을 설정해야 합니다.")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
