"""What produced an episode: code, image, PX4 build and scenario. Recorded in every `episode.json`."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lost_in_place import __version__

REPO_ROOT = Path(__file__).resolve().parents[2]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_sha(env: Mapping[str, str] = os.environ) -> str:
    if env.get("LIP_GIT_SHA"):
        return env["LIP_GIT_SHA"]
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return out.stdout.strip() or "unknown"


def image_digest(env: Mapping[str, str] = os.environ) -> str:
    """The running container's image digest, from the ECS task metadata endpoint (Batch on EC2)."""
    uri = env.get("ECS_CONTAINER_METADATA_URI_V4")
    if not uri:
        return env.get("LIP_IMAGE_DIGEST", "")
    try:
        with urllib.request.urlopen(uri, timeout=2) as response:
            meta = json.load(response)
    except (OSError, ValueError):
        return ""
    return digest_from_task_metadata(meta)


def digest_from_task_metadata(meta: Mapping[str, Any]) -> str:
    """The registry digest when the job pinned one (`repo@sha256:...`), so it matches the job definition.

    ECS's `ImageID` is the local image config digest, which never appears in ECR or the job definition.
    """
    image = str(meta.get("Image", ""))
    if "@sha256:" in image:
        return image.split("@", 1)[1]
    return str(meta.get("ImageID", ""))


def px4_build(px4_dir: Path = REPO_ROOT / "px4") -> dict[str, Any]:
    commit_file = px4_dir / "PX4_COMMIT"
    return {
        "commit": commit_file.read_text().strip() if commit_file.exists() else "unknown",
        "patches": {p.name: sha256(p.read_bytes())[:16] for p in sorted(px4_dir.glob("patches/*.patch"))},
    }


def collect(scenario_json: str, env: Mapping[str, str] = os.environ) -> dict[str, Any]:
    return {
        "lip_version": __version__,
        "git_sha": git_sha(env),
        "image_digest": image_digest(env),
        "px4": px4_build(),
        "scenario_sha256": sha256(scenario_json.encode())[:16],
    }
