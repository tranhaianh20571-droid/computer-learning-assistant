"""Start the official Hindsight service with an isolated local configuration."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import subprocess
import sys
import hashlib
import time
import urllib.request


def watch(config_path):
    """Only restart the child owned by this supervisor, never a port's owner."""
    from dotenv import dotenv_values
    child, fingerprint = None, None
    try:
        while True:
            current = hashlib.sha256(config_path.read_bytes()).hexdigest()
            if current != fingerprint or child is None or child.poll() is not None:
                if child and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=10)
                config = dotenv_values(config_path)
                url = config.get("MEMORY_API_URL", "http://127.0.0.1:9078").rstrip("/")
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                          "--config", str(config_path)])
                fingerprint = current
                for _ in range(90):
                    if child.poll() is not None:
                        break
                    try:
                        with urllib.request.urlopen(url + "/health", timeout=2) as response:
                            healthy = response.status == 200
                        if healthy:
                            target = config_path.with_name(config_path.name + ".applied")
                            temporary = target.with_name(target.name + ".tmp")
                            temporary.write_text(current, encoding="utf8")
                            os.replace(temporary, target)
                            break
                    except Exception:
                        pass
                    time.sleep(1)
            time.sleep(2)
    finally:
        if child and child.poll() is None:
            child.terminate()
            child.wait(timeout=25)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--secure", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--configure-personal", action="store_true",
        help="Create a separate personal-model runtime config, never overwrite an existing file.")
    parser.add_argument("--application-url", default="http://127.0.0.1:8000")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "server" / ".env.memory")
    args = parser.parse_args()
    from dotenv import dotenv_values
    if args.configure_personal:
        import secrets
        from urllib.parse import urlsplit
        url = urlsplit(args.application_url)
        if url.scheme not in ("http", "https") or url.hostname not in ("127.0.0.1", "::1", "localhost") or url.query or url.fragment:
            raise SystemExit("Personal gateway must use an explicit loopback application address.")
        target = args.config
        if target.exists():
            raise SystemExit("Configuration already exists; refusing to overwrite.")
        proxy_key, service_key = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        proxy = args.application_url.rstrip("/") + "/api/internal/memory"
        data = {
            "MEMORY_API_URL": "http://127.0.0.1:9079", "MEMORY_API_KEY": service_key,
            "MEMORY_PROVIDER_PROXY_KEY": proxy_key, "HINDSIGHT_API_PORT": "9079",
            "HINDSIGHT_API_DATABASE_URL": "pg0://learning-assistant-personal-memory",
            "HINDSIGHT_API_TENANT_API_KEY": service_key,
            "HINDSIGHT_API_TENANT_EXTENSION": "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension",
            "HINDSIGHT_API_LLM_PROVIDER": "openai",
            "HINDSIGHT_API_LLM_BASE_URL": proxy + "/v1",
            "HINDSIGHT_API_LLM_MODEL": "personal-memory",
            "HINDSIGHT_API_LLM_API_KEY": proxy_key,
            "HINDSIGHT_API_LLM_SEND_BANK_AS_USER": "true",
            "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": proxy + "/v1",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": "personal-memory",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": proxy_key,
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS": "2000",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE": "32",
            "HINDSIGHT_API_RERANKER_PROVIDER": "siliconflow",
            "HINDSIGHT_API_RERANKER_SILICONFLOW_BASE_URL": proxy + "/v1",
            "HINDSIGHT_API_RERANKER_SILICONFLOW_MODEL": "personal-memory",
            "HINDSIGHT_API_RERANKER_SILICONFLOW_API_KEY": proxy_key,
            "HINDSIGHT_API_RERANKER_SEND_BANK_AS_HEADER": "true",
            "HINDSIGHT_API_RERANKER_MAX_CANDIDATES": "40",
            "HINDSIGHT_API_ACCESS_LOG": "false", "HINDSIGHT_API_LOG_LEVEL": "warning",
            "HINDSIGHT_API_MCP_ENABLED": "false", "HINDSIGHT_API_LLM_OUTPUT_LANGUAGE": "Chinese",
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf8") as stream:
            for name, value in sorted(data.items()):
                stream.write(name + "='" + value + "'\n")
        print("Separate personal-model runtime configuration created; credentials hidden.")
        return
    desktop = os.environ.get("STUDY_MEMORY_DESKTOP") == "1"
    config = ({k: v for k, v in os.environ.items() if k.startswith("HINDSIGHT_API_")}
              if desktop else {k: v for k, v in dotenv_values(args.config).items() if v is not None})
    if args.watch:
        watch(args.config.resolve())
        return
    if args.secure:
        import secrets
        from dotenv import set_key
        key = config.get("MEMORY_API_KEY") or secrets.token_urlsafe(48)
        for name, value in {
            "MEMORY_API_KEY": key,
            "HINDSIGHT_API_TENANT_API_KEY": key,
            "HINDSIGHT_API_TENANT_EXTENSION": "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension",
        }.items():
            set_key(args.config, name, value)
        print("Native Hindsight service authentication configured.")
        return
    if args.start:
        runtime = Path(__file__).resolve().parents[1] / ".runtime" / "product-memory"
        runtime.mkdir(parents=True, exist_ok=True)
        with (runtime / "stdin").open("w") as empty:
            pass
        with (runtime / "stdin").open("r") as stdin, (runtime / "service.log").open("ab") as output:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watch", "--config", str(args.config)],
                stdin=stdin, stdout=output, stderr=output, creationflags=flags)
        print(f"Product memory starting (PID {process.pid}).")
        return
    # Product secrets and private content must not enter third-party diagnostics.
    logging.disable(logging.CRITICAL)
    os.environ.update(config)
    os.environ["HINDSIGHT_API_LLM_TRACE_ENABLED"] = "false"
    os.environ["PYDANTIC_AI_NO_BANNER"] = "1"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "backend"))
    from app.memory.native_lifecycle import install as install_lifecycle
    install_lifecycle()
    if config.get("MEMORY_PROVIDER_PROXY_KEY") or desktop:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "backend"))
        from app.memory.hindsight_runtime import install_bank_attribution
        install_bank_attribution()
    from hindsight_api.main import main as run_api
    sys.argv = ["hindsight-api", "--host", "127.0.0.1", "--port", config.get("HINDSIGHT_API_PORT", "9078")]
    run_api()


if __name__ == "__main__":
    main()
