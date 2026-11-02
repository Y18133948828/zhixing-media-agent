from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env", extra="ignore", hide_input_in_errors=True
    )
    database_url: str = Field("postgresql://media:change-me@127.0.0.1:5432/media_ops", repr=False)
    database_mode: Literal["portable", "shared"] = "shared"
    checkpointer_backend: Literal["postgres", "memory"] = "postgres"
    api_key: SecretStr = SecretStr("replace-with-a-long-random-key")
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    storage_dir: Path = BACKEND_ROOT / "storage"
    llm_provider: Literal["mock", "openai_compatible", "anthropic", "volcengine"] = "mock"
    llm_base_url: str = "https://your-provider.example/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "your-model-name"
    llm_small_model: str = ""
    llm_json_mode: bool = True
    llm_timeout_seconds: int = Field(90, ge=5, le=300)
    llm_max_tokens: int = Field(6000, ge=1000, le=32000)
    max_revisions: int = Field(10, ge=1, le=100)
    worker_poll_seconds: float = Field(1, ge=0.1)
    job_timeout_seconds: int = Field(600, ge=30)
    jwt_secret: SecretStr = SecretStr("")
    jwt_hours: int = Field(12, ge=1, le=168)
    llm_streaming: bool = True
    llm_input_price_per_million: float | None = Field(None, ge=0)
    llm_output_price_per_million: float | None = Field(None, ge=0)
    image_provider: Literal["template", "openai_compatible", "gemini", "volcengine"] = "template"
    image_base_url: str = ""
    image_api_key: SecretStr = SecretStr("")
    image_model: str = ""
    image_auth: Literal["bearer", "x-goog-api-key"] = "bearer"
    image_size: str = "1536x2048"
    image_concurrency: int = Field(2, ge=1, le=5)
    image_timeout_seconds: int = Field(120, ge=10, le=300)
    redact_pii: bool = True
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_api_key: SecretStr = SecretStr("")
    ark_text_model: str = ""
    ark_image_model: str = ""
    ark_thinking: Literal["disabled", "enabled", "omit"] = "disabled"
    ark_image_size: str = "1728x2304"
    ark_image_response_format: Literal["b64_json", "url"] = "b64_json"
    ark_image_watermark: bool = True
    database_pool_min: int = Field(1, ge=1, le=20)
    database_pool_max: int = Field(5, ge=1, le=100)
    database_business_pool: int = Field(5, ge=1, le=50)
    node_timeouts: dict[str, int] = {
        "topics": 300,
        "write_article": 300,
        "summarize": 180,
        "render_images": 480,
        "wechat_format": 30,
    }
    stage_temperatures: dict[str, float | None] = {"TopicBatch": 0.8, "Article": 0.4, "CardPack": 0.2}
    stage_max_tokens: dict[str, int] = {"TopicBatch": 1800, "Article": 4000, "CardPack": 3000}
    llm_attempts: int = Field(3, ge=1, le=5)
    llm_cache_seconds: int = Field(0, ge=0, le=3600)
    llm_fallback_provider: Literal["disabled", "openai_compatible", "volcengine", "anthropic"] = (
        "disabled"
    )
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: SecretStr = SecretStr("")
    llm_fallback_model: str = ""
    image_price_per_image: float | None = Field(None, ge=0)
    cost_currency: str = Field("CNY", pattern="^[A-Z]{3}$")
    rate_limit_per_minute: int = Field(300, ge=1, le=100000)
    login_limit_per_minute: int = Field(10, ge=1, le=1000)
    log_dir: Path = BACKEND_ROOT / "logs"
    tracing_enabled: bool = False
    tracing_include_content: bool = False
    tracing_api_key: SecretStr = SecretStr("")
    tracing_endpoint: str = "https://api.smith.langchain.com"
    tracing_project: str = "media-demo"
    content_review_url: str = ""
    content_review_key: SecretStr = SecretStr("")
    content_blocked_terms: list[str] = []
    embedding_provider: Literal["disabled", "openai_compatible", "volcengine"] = "disabled"
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = ""
    embedding_batch_size: int = Field(16, ge=1, le=64)
    embedding_concurrency: int = Field(4, ge=1, le=10)
    embedding_price_per_million: float | None = Field(None, ge=0)
    ark_embedding_model: str = ""
    ark_embedding_dimensions: Literal[1024, 2048] | None = None
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_redirect_uri: str = "http://127.0.0.1:8000/api/v1/auth/sso/callback"
    cookie_secure: bool = False
    routing_strategy: Literal["stage", "adaptive"] = "stage"
    routing_latency_budget_seconds: float = Field(15, ge=1, le=300)
    routing_cost_budget: float | None = Field(None, ge=0)

    @property
    def text_base_url(self):
        return self.ark_base_url if self.llm_provider == "volcengine" else self.llm_base_url

    @property
    def text_api_key(self):
        return self.ark_api_key if self.llm_provider == "volcengine" else self.llm_api_key

    @property
    def text_model(self):
        return self.ark_text_model if self.llm_provider == "volcengine" else self.llm_model

    @property
    def image_dimensions(self):
        size = self.ark_image_size if self.image_provider == "volcengine" else self.image_size
        if self.image_provider == "template":
            return [1242, 1656]
        if self.image_provider == "gemini":
            return None
        import re

        match = re.fullmatch(r"([1-9]\d*)x([1-9]\d*)", size)
        return [int(match[1]), int(match[2])] if match else None

    @model_validator(mode="after")
    def validate_settings(self):
        if self.checkpointer_backend == "memory" and not self.database_url.rsplit("/", 1)[-1].endswith(
            "_test"
        ):
            raise ValueError("内存检查点仅允许在独立的_test数据库中用于调试；正常Demo使用postgres")
        if self.database_pool_min > self.database_pool_max:
            raise ValueError("数据库连接池最小值不能超过最大值")
        if any(not 5 <= v <= 1800 for v in self.node_timeouts.values()):
            raise ValueError("节点超时需在5–1800秒之间")
        if any(v is not None and not 0 <= v <= 2 for v in self.stage_temperatures.values()):
            raise ValueError("temperature需在0–2之间，或使用null省略")
        if any(not 256 <= v <= 32000 for v in self.stage_max_tokens.values()):
            raise ValueError("阶段输出上限需在256–32000之间")
        if self.tracing_enabled and not self.tracing_api_key.get_secret_value():
            raise ValueError("启用外部追踪需要TRACING_API_KEY")
        if self.llm_fallback_provider != "disabled":
            if (
                not self.llm_fallback_model
                or (
                    self.llm_fallback_provider != "volcengine"
                    and not self.llm_fallback_api_key.get_secret_value()
                )
                or (
                    self.llm_fallback_provider == "volcengine"
                    and not self.ark_api_key.get_secret_value()
                )
            ):
                raise ValueError("备用模型需要配置模型ID和对应凭据")
        if self.embedding_provider == "openai_compatible" and not (
            self.embedding_base_url
            and self.embedding_model
            and self.embedding_api_key.get_secret_value()
        ):
            raise ValueError("向量检索需要配置Embedding地址、模型和凭据")
        if self.embedding_provider == "volcengine" and not (
            self.ark_embedding_model and self.ark_api_key.get_secret_value()
        ):
            raise ValueError("火山向量模型需要ARK_EMBEDDING_MODEL和ARK_API_KEY")
        if self.oidc_issuer and not (self.oidc_issuer.startswith("https://") and self.oidc_client_id):
            raise ValueError("SSO需要HTTPS的OIDC Issuer和Client ID")
        if self.oidc_issuer:
            from urllib.parse import urlsplit

            issuer = urlsplit(self.oidc_issuer)
            redirect = urlsplit(self.oidc_redirect_uri)
            if issuer.username or issuer.password or issuer.query or issuer.fragment:
                raise ValueError("OIDC Issuer不能包含凭据、查询参数或片段")
            if redirect.scheme != "https" and not (
                redirect.scheme == "http" and redirect.hostname in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("OIDC回调需使用HTTPS，只有本机回调可用HTTP")
            if redirect.scheme == "https" and not self.cookie_secure:
                raise ValueError("HTTPS回调需要COOKIE_SECURE=true")
        if not self.database_url.startswith("postgresql://"):
            raise ValueError("DATABASE_URL 必须使用 postgresql:// 格式")
        if len(self.api_key.get_secret_value()) < 16:
            raise ValueError("API_KEY 至少 16 个字符")
        if self.llm_provider not in ("mock", "volcengine") and not self.llm_api_key.get_secret_value():
            raise ValueError("真实模型模式必须设置 LLM_API_KEY")
        if self.llm_provider == "volcengine" or self.image_provider == "volcengine":
            from urllib.parse import urlsplit

            base = urlsplit(self.ark_base_url)
            if (
                base.scheme != "https"
                or not base.hostname
                or base.username
                or base.password
                or base.query
                or base.fragment
                or base.path.rstrip("/") != "/api/v3"
            ):
                raise ValueError(
                    "ARK_BASE_URL 需为 https://主机/api/v3，不要填完整调用路径或 Coding Plan 地址"
                )
            if not self.ark_api_key.get_secret_value().strip():
                raise ValueError("火山方舟模式必须设置 ARK_API_KEY")
        if self.llm_provider == "volcengine" and not self.ark_text_model.strip():
            raise ValueError("火山方舟文字模型需要 ARK_TEXT_MODEL（Model ID 或 Endpoint ID）")
        if self.image_provider == "volcengine":
            if not self.ark_image_model.strip():
                raise ValueError("火山方舟图片模型需要 ARK_IMAGE_MODEL（Model ID 或 Endpoint ID）")
            dimensions = self.image_dimensions
            if not dimensions or dimensions[0] * 4 != dimensions[1] * 3:
                raise ValueError("ARK_IMAGE_SIZE 需为3:4像素尺寸，例如 1728x2304")
            if not 3686400 <= dimensions[0] * dimensions[1] <= 16777216:
                raise ValueError("ARK_IMAGE_SIZE 总像素需在 3686400–16777216 之间；Demo 使用 1728x2304")
        if not self.storage_dir.is_absolute():
            self.storage_dir = (BACKEND_ROOT / self.storage_dir).resolve()
        if not self.log_dir.is_absolute():
            self.log_dir = (BACKEND_ROOT / self.log_dir).resolve()
        return self
