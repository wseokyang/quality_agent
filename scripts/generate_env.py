#!/usr/bin/env python3
"""Create a private deployment env; no secret is printed or overwritten."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets


def generate(destination: Path, environment: str = "production") -> None:
    db_password = secrets.token_hex(32)
    values = {
        "QUALITY_ENVIRONMENT": environment,
        "QUALITY_IMAGE": "quality-agent:local",
        "QUALITY_PYTHON_BASE_IMAGE": "python:3.12-slim",
        "QUALITY_POSTGRES_IMAGE": "postgres:16",
        "QUALITY_BIND_HOST": "127.0.0.1",
        "QUALITY_HTTP_PORT": "18080",
        "QUALITY_DB_PASSWORD": db_password,
        "QUALITY_DATABASE_URL": f"postgresql+psycopg://quality:{db_password}@db:5432/quality",
        "QUALITY_HUMAN_JWT_SECRET": secrets.token_urlsafe(48),
        "QUALITY_MODEL_JWT_SECRET": secrets.token_urlsafe(48),
        "QUALITY_TRACE_JWT_SECRET": secrets.token_urlsafe(48),
        "QUALITY_WORKER_JWT_SECRET": secrets.token_urlsafe(48),
        "QUALITY_RECOVERY_EPOCH": secrets.token_hex(16),
        "QUALITY_RECOVERY_MODE": "false",
        "QUALITY_DRAINING": "false",
        "QUALITY_JWT_ISSUER": "quality-openwebui",
        "QUALITY_JWT_AUDIENCE": "quality-api",
        "QUALITY_USER_TEAMS_JSON": "{}",
        "QUALITY_CURATOR_IDS_JSON": "[]",
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("# Generated locally; keep out of git, chat, and image layers.\n")
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
    print(f"Created private environment file: {destination}. Values were not printed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument("--environment", choices=("production", "development"), default="production")
    args = parser.parse_args()
    try:
        generate(args.output, args.environment)
    except FileExistsError:
        parser.exit(2, "Refusing to overwrite an existing environment file.\n")


if __name__ == "__main__":
    main()
