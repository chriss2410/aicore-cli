"""Docker build & push helpers."""

import subprocess
import sys
from pathlib import Path

from aic import ui

DEFAULT_DOCKERFILE = "vllm.dockerfile"


def is_running() -> bool:
    """Return True if the Docker daemon is reachable."""
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
    )
    return result.returncode == 0


def build(image_tag: str, dockerfile: str = DEFAULT_DOCKERFILE, context: str = ".") -> bool:
    """Build a Docker image for linux/amd64."""
    ui.dim(f"  Building {image_tag} from {dockerfile}...")
    cmd = [
        "docker", "build",
        "-t", image_tag,
        "--platform", "linux/amd64",
        "-f", dockerfile,
        context,
    ]
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        ui.error(f"Docker build failed (exit {result.returncode})")
        return False
    ui.success(f"Built {image_tag}")
    return True


def push(image_tag: str) -> bool:
    """Push a Docker image to registry."""
    ui.dim(f"  Pushing {image_tag}...")
    result = subprocess.run(["docker", "push", image_tag], capture_output=False)
    if result.returncode != 0:
        ui.error(f"Docker push failed (exit {result.returncode})")
        return False
    ui.success(f"Pushed {image_tag}")
    return True


def image_exists_locally(image_tag: str) -> bool:
    """Check if a Docker image exists locally."""
    result = subprocess.run(
        ["docker", "image", "inspect", image_tag],
        capture_output=True,
    )
    return result.returncode == 0
