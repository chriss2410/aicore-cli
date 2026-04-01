"""AI Core client factory. Loads credentials from JSON key files."""

import json
import os
import sys
from pathlib import Path

_AIC_HOME = Path.home() / ".aic"
_CONFIG_FILE = _AIC_HOME / "config"


def secrets_dir() -> Path:
    """Resolve the credentials directory.

    Priority:
      1. AIC_SECRETS_DIR environment variable  (CI/CD or per-session override)
      2. Path saved in ~/.aic/config by 'aic setup'
      3. ~/.aic/                               (fallback default)
    """
    if env := os.environ.get("AIC_SECRETS_DIR"):
        return Path(env)
    if _CONFIG_FILE.exists():
        saved = _CONFIG_FILE.read_text().strip()
        if saved:
            return Path(saved)
    return _AIC_HOME


def save_secrets_dir(path: Path) -> None:
    """Persist the credentials directory to ~/.aic/config."""
    _AIC_HOME.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(str(path.expanduser().resolve()) + "\n")
    os.chmod(_CONFIG_FILE, 0o600)


# Callers import these and call them to get the current path.
# They are functions (not module-level Path values) so the resolution
# happens at call time, after the working directory is known.
def DEFAULT_AIC_KEY()    -> Path: return secrets_dir() / "aic-key.json"
def DEFAULT_S3_KEY()     -> Path: return secrets_dir() / "s3-key.json"
def DEFAULT_DOCKER_KEY() -> Path: return secrets_dir() / "docker-secret.json"
def DEFAULT_GITHUB_KEY() -> Path: return secrets_dir() / "github-key.json"
def DEFAULT_ARGOCD_KEY() -> Path: return secrets_dir() / "argocd-key.json"


_aic_creds: dict | None = None


def _load_aic_key(path: Path | None = None) -> dict:
    """Load AI Core service key JSON."""
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
    """Create an AICoreV2Client from a JSON service key file."""
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
