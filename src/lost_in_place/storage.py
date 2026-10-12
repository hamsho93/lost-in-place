"""Upload finished episodes to S3 (used by the AWS Batch jobs; needs the `aws` extra)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"expected s3://bucket/prefix, got {uri!r}")
    return parsed.netloc, parsed.path.strip("/")


def attempt_label(env: Mapping[str, str] = os.environ) -> str:
    """Name one run of an episode, so retries and reruns never overwrite each other's uploads."""
    job = env.get("AWS_BATCH_JOB_ID")
    if job:
        return f"{job.replace(':', '-')}-attempt{env.get('AWS_BATCH_JOB_ATTEMPT', '1')}"
    return datetime.now(UTC).strftime("local-%Y%m%dT%H%M%SZ")


def upload_prefix(prefix: str, local: Path, label: str) -> str:
    return "/".join(part for part in (prefix, local.name, label) if part)


def upload_dir(local: Path, uri: str, label: str) -> str:
    """Upload every file under `local` to `uri`/<local name>/<label>/...; returns where they went."""
    import boto3  # imported lazily so the core package doesn't require it

    bucket, prefix = parse_s3_uri(uri)
    dest = upload_prefix(prefix, local, label)
    s3 = boto3.client("s3")
    for path in sorted(p for p in local.rglob("*") if p.is_file()):
        s3.upload_file(str(path), bucket, f"{dest}/{path.relative_to(local).as_posix()}")
    return f"s3://{bucket}/{dest}/"
