"""Upload finished episodes to S3 (used by the AWS Batch jobs; needs the `aws` extra)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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


def upload_prefix(prefix: str, local: Path, label: str, name: str | None = None) -> str:
    return "/".join(part for part in (prefix, name or local.name, label) if part)


def upload_dir(local: Path, uri: str, label: str, name: str | None = None) -> str:
    """Upload every file under `local` to `uri`/<name>/<label>/...; returns where they went.

    `name` defaults to the directory's own name; manifest runs pass the episode key.
    """
    import boto3  # imported lazily so the core package doesn't require it

    bucket, prefix = parse_s3_uri(uri)
    dest = upload_prefix(prefix, local, label, name)
    s3 = boto3.client("s3")
    for path in sorted(p for p in local.rglob("*") if p.is_file()):
        s3.upload_file(str(path), bucket, f"{dest}/{path.relative_to(local).as_posix()}")
    return f"s3://{bucket}/{dest}/"


def get_json(bucket: str, key: str) -> Any:
    import boto3

    return json.loads(boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read())


def list_files(uri: str, suffixes: tuple[str, ...]) -> list[tuple[str, str, datetime]]:
    """(bucket, key, last modified) of every object under `uri` ending in one of `suffixes`."""
    import boto3

    bucket, prefix = parse_s3_uri(uri)
    pages = (
        boto3.client("s3")
        .get_paginator("list_objects_v2")
        .paginate(Bucket=bucket, Prefix=f"{prefix}/" if prefix else "")
    )
    return [
        (bucket, obj["Key"], obj["LastModified"])
        for page in pages
        for obj in page.get("Contents", [])
        if obj["Key"].endswith(suffixes)
    ]


def attempt_outcomes(uri: str, key: str) -> list[str]:
    """Outcomes of the attempts already uploaded for one episode key under `uri`."""
    return [
        str(get_json(bucket, obj).get("outcome", "pending"))
        for bucket, obj, _ in list_files(f"{uri.rstrip('/')}/{key}", ("/episode.json",))
    ]
