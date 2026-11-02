"""HISTORY_* settings of agents-history.

A plain pydantic-settings class, not agents-common's BaseServiceSettings: that base demands an
orchestrator url and token, which the api never needs. CsvList and SettingsError are shared with
the other services. The retention period lives in the settings row, not here.
"""

import ipaddress

from agents_common.settings import CsvList, SettingsError
from pydantic import (
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = ["Settings", "SettingsError", "load_settings"]

PREFIX = "HISTORY_"


class Settings(BaseSettings):
    """Environment-driven settings shared by every process (api, migrate)."""

    model_config = SettingsConfigDict(env_prefix=PREFIX, extra="ignore")

    database_url: SecretStr = Field(repr=False)
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    service_port: int = 8001
    service_tokens_file: str | None = None
    ingest_port: int = 8002
    ingest_tokens_file: str | None = None
    allowed_cidrs: CsvList = ["127.0.0.1/32"]
    log_level: str = "INFO"
    shutdown_grace_seconds: float = 20
    search_timeout_seconds: float = Field(8, ge=1, le=60)
    sweep_interval_seconds: int = Field(3600, ge=60, le=86400)
    sweep_batch: int = Field(50, ge=1, le=1000)
    ingest_max_line_bytes: int = Field(67108864, ge=1048576, le=268435456)

    @field_validator("*", mode="before")
    @classmethod
    def _split_csv(cls, value: object, info: ValidationInfo) -> object:
        field = cls.model_fields.get(info.field_name or "")
        if field is not None and NoDecode in field.metadata and isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("allowed_cidrs")
    @classmethod
    def _cidrs(cls, value: list[str], info: ValidationInfo) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(
                    f"{PREFIX}{str(info.field_name).upper()}::not a CIDR: {item!r}"
                ) from None
        return value

    @model_validator(mode="after")
    def _distinct_ports(self) -> "Settings":
        """Every enabled listener needs its own port."""
        taken = {"HTTP_PORT": self.http_port}
        for enabled, name, port in (
            (self.service_tokens_file, "SERVICE_PORT", self.service_port),
            (self.ingest_tokens_file, "INGEST_PORT", self.ingest_port),
        ):
            if not enabled:
                continue
            for other, other_port in taken.items():
                if port == other_port:
                    raise ValueError(f"{PREFIX}{name}::must differ from {PREFIX}{other}")
            taken[name] = port
        return self

    def api_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in self.allowed_cidrs]


def load_settings() -> Settings:
    """Load Settings from the environment or raise SettingsError naming every bad variable."""
    try:
        return Settings()
    except ValidationError as exc:
        problems: dict[str, str] = {}
        for err in exc.errors():
            msg = err["msg"].removeprefix("Value error, ")
            var, sep, rest = msg.partition("::")
            if sep and var.startswith(PREFIX):
                problems.setdefault(var, rest)
                continue
            msg = "required" if err["type"] == "missing" else msg
            loc = err["loc"]
            name = f"{PREFIX}{str(loc[0]).upper()}" if loc else f"{PREFIX}*"
            problems.setdefault(name, msg)
        raise SettingsError(problems) from None
