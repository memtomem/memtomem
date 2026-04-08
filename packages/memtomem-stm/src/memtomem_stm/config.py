"""STM (Short-Term Memory) root configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pydantic import BaseModel

from memtomem_stm.proxy.config import ProxyConfig
from memtomem_stm.surfacing.config import SurfacingConfig


class LangfuseConfig(BaseModel):
    """Langfuse tracing configuration."""

    enabled: bool = False
    public_key: str = ""
    secret_key: str = ""
    host: str = ""


class STMConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMTOMEM_STM_",
        env_nested_delimiter="__",
    )

    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    surfacing: SurfacingConfig = Field(default_factory=SurfacingConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    data_dir: Path = Path("~/.memtomem")

    def model_post_init(self, __context: object) -> None:
        # Propagate consumer_model from proxy to surfacing for model-aware defaults
        if self.proxy.consumer_model and not self.surfacing.consumer_model:
            self.surfacing.consumer_model = self.proxy.consumer_model
