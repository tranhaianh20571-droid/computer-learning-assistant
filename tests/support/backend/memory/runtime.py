"""Deployment secrets for the private provider gateway, never browser configuration."""
import os
from pathlib import Path
from dotenv import dotenv_values


def personal_runtime():
    path = Path(os.environ.get("PERSONAL_MEMORY_CONFIG_FILE",
        Path(__file__).resolve().parents[3] / ".env.personal-memory"))
    values = dotenv_values(path) if path.is_file() else {}
    return {
        "url": os.environ.get("PERSONAL_MEMORY_API_URL", values.get("MEMORY_API_URL", "")),
        "key": os.environ.get("PERSONAL_MEMORY_API_KEY", values.get("MEMORY_API_KEY", "")),
        "proxy_key": os.environ.get("MEMORY_PROVIDER_PROXY_KEY",
            values.get("MEMORY_PROVIDER_PROXY_KEY", "")),
    }
