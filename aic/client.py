"""AI Core client factory. Loads credentials from JSON key files."""

import json
import os
import sys
from pathlib import Path

_AIC_HOME = Path.home() / ".aic"
_CONFIG_FILE = _AIC_HOME / "config"


# ---------------------------------------------------------------------------
# ~/.aic/config  (key=value, one per line)
# ---------------------------------------------------------------------------

def _read_config() -> dict:
    if not _CONFIG_FILE.exists():
        return {}
    result = {}
    for line in _CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_config(data: dict) -> None:
    _AIC_HOME.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in sorted(data.items())]
    _CONFIG_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(_CONFIG_FILE, 0o600)


def _save_config_value(key: str, value: str) -> None:
    data = _read_config()
    data[key] = value
    _write_config(data)


# ---------------------------------------------------------------------------
# Credentials directory
# ---------------------------------------------------------------------------

def secrets_dir() -> Path:
    """Priority: AIC_SECRETS_DIR env → ~/.aic/config → ~/.aic/"""
    if env := os.environ.get("AIC_SECRETS_DIR"):
        return Path(env)
    cfg = _read_config()
    if "secrets_dir" in cfg:
        return Path(cfg["secrets_dir"])
    return _AIC_HOME


def save_secrets_dir(path: Path) -> None:
    _save_config_value("secrets_dir", str(path.expanduser().resolve()))


# ---------------------------------------------------------------------------
# Templates directory  (where app/*.yaml ServingTemplates live)
# ---------------------------------------------------------------------------

def templates_dir() -> Path:
    """Return the configured templates directory, defaulting to ./app."""
    cfg = _read_config()
    if "templates_dir" in cfg:
        return Path(cfg["templates_dir"])
    return Path("app")


def save_templates_dir(path: Path) -> None:
    _save_config_value("templates_dir", str(path))


# ---------------------------------------------------------------------------
# Per-key defaults  (resolved at call time, not import time)
# ---------------------------------------------------------------------------

def DEFAULT_AIC_KEY()    -> Path: return secrets_dir() / "aic-key.json"
def DEFAULT_S3_KEY()     -> Path: return secrets_dir() / "s3-key.json"
def DEFAULT_DOCKER_KEY() -> Path: return secrets_dir() / "docker-secret.json"
def DEFAULT_GITHUB_KEY() -> Path: return secrets_dir() / "github-key.json"
def DEFAULT_ARGOCD_KEY() -> Path: return secrets_dir() / "argocd-key.json"


# ---------------------------------------------------------------------------
# AI Core client
# ---------------------------------------------------------------------------

_aic_creds: dict | None = None


def _load_aic_key(path: Path | None = None) -> dict:
    global _aic_creds
    if _aic_creds is not None:
        return _aic_creds
    resolved = path or DEFAULT_AIC_KEY()
    if not resolved.exists():
        print(f"Error: AI Core key file not found: {resolved}")
        print(f"Run 'aic setup' or place the key at {resolved}")
        print("Download from: BTP Cockpit → AI Core instance → Service Keys")
        sys.exit(1)
    with open(resolved) as f:
        _aic_creds = json.load(f)
    return _aic_creds


def create_client(resource_group: str = None, aic_key_path: Path = None):
    from ai_core_sdk.ai_core_v2_client import AICoreV2Client
    creds = _load_aic_key(aic_key_path)
    return AICoreV2Client(
        base_url=creds["serviceurls"]["AI_API_URL"] + "/v2",
        auth_url=creds["url"] + "/oauth/token",
        client_id=creds["clientid"],
        client_secret=creds["clientsecret"],
        resource_group=resource_group or "default",
    )


def get_resource_group() -> str:
    return "default"
