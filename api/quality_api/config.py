from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUALITY_", extra="ignore", hide_input_in_errors=True)

    environment: str = "production"
    database_url: str = Field(default="", repr=False)
    artifact_dir: Path = Path("/var/lib/quality/artifacts")
    human_jwt_secret: str = Field(default="", repr=False)
    model_jwt_secret: str = Field(default="", repr=False)
    trace_jwt_secret: str = Field(default="", repr=False)
    worker_jwt_secret: str = Field(default="", repr=False)
    jwt_issuer: str = "quality-openwebui"
    jwt_audience: str = "quality-api"
    recovery_epoch: str = ""
    tool_registry_json: str = "[]"
    tool_registry_path: Path | None = None
    user_teams_json: str = "{}"
    curator_ids_json: str = "[]"
    allow_sqlite: bool = False
    max_request_bytes: int = 2_000_000
    token_ttl_seconds: int = 300
    ui_ticket_ttl_seconds: int = 60
    draining: bool = False
    recovery_mode: bool = False

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.environment not in {"production", "development", "test"}:
            raise ValueError("QUALITY_ENVIRONMENT must be production, development or test")
        if not self.database_url:
            raise ValueError("QUALITY_DATABASE_URL is required")
        if self.database_url.startswith("sqlite"):
            if self.environment == "production" or not self.allow_sqlite:
                raise ValueError("SQLite is allowed only in explicit test/development mode")
        elif not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("Use a postgresql+psycopg:// database URL")
        secrets = list(self.signing_keys.values())
        if any(len(value.encode()) < 32 for value in secrets):
            raise ValueError("All four role-specific JWT secrets must be at least 32 bytes")
        if len(set(secrets)) != len(secrets):
            raise ValueError("Role-specific JWT secrets must be different")
        if not self.recovery_epoch or len(self.recovery_epoch) > 80:
            raise ValueError("QUALITY_RECOVERY_EPOCH must be a nonempty identifier")
        if not 30 <= self.token_ttl_seconds <= 300:
            raise ValueError("Token lifetime must be 30..300 seconds")
        if not 10 <= self.ui_ticket_ttl_seconds <= 120:
            raise ValueError("UI ticket lifetime must be 10..120 seconds")
        self.tool_registry
        self.user_teams
        self.curator_ids
        return self

    @property
    def signing_keys(self) -> dict[str, str]:
        return {"human": self.human_jwt_secret, "model_delegate": self.model_jwt_secret,
                "trace_adapter": self.trace_jwt_secret, "worker": self.worker_jwt_secret}

    @property
    def tool_registry(self) -> dict[str, dict]:
        raw = self.tool_registry_path.read_text() if self.tool_registry_path else self.tool_registry_json
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise ValueError("Tool registry must be a list")
        result = {}
        for entry in entries:
            if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) and entry[k] for k in ("id", "version", "effect")):
                raise ValueError("Registry entries require id, version, effect")
            if entry["id"] in result or entry["effect"] not in {"read", "write", "unknown"}:
                raise ValueError("Duplicate tool or unsupported effect")
            if not isinstance(entry.get("shareable", False), bool):
                raise ValueError("Registry shareable must be boolean")
            schema = entry.get("input_schema", {"type": "object"})
            # Registry schemas are administrator supplied; remote refs remain disabled.
            def reject_remote(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        if key in {"$ref", "$dynamicRef"} and (not isinstance(value, str) or not value.startswith("#")):
                            raise ValueError("Remote schema references are forbidden")
                        reject_remote(value)
                elif isinstance(node, list):
                    for value in node:
                        reject_remote(value)
            reject_remote(schema)
            Draft202012Validator.check_schema(schema)
            result[entry["id"]] = {**entry, "input_schema": schema, "shareable": entry.get("shareable", False)}
        return result

    @property
    def user_teams(self) -> dict[str, list[str]]:
        value = json.loads(self.user_teams_json)
        if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, list) or any(not isinstance(t, str) or not t for t in v) for k, v in value.items()):
            raise ValueError("User teams must map user IDs to lists of team IDs")
        return value

    @property
    def curator_ids(self) -> set[str]:
        value = json.loads(self.curator_ids_json)
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError("Curator IDs must be a list")
        return set(value)
