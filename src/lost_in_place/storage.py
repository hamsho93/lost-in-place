"""Upload finished episodes to S3 (used by the AWS Batch jobs; needs the `aws` extra)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"expected s3://bucket/prefix, got {uri!r}")
    return parsed.netloc, parsed.path.strip("/")


def upload_dir(local: Path, uri: str) -> int:
    """Upload every file under `local` to `uri`/<local name>/...; returns the number of files."""
    import boto3  # imported lazily so the core package doesn't require it

    bucket, prefix = parse_s3_uri(uri)
    s3 = boto3.client("s3")
    count = 0
    for path in sorted(p for p in local.rglob("*") if p.is_file()):
        key = "/".join(part for part in (prefix, local.name, path.relative_to(local).as_posix()) if part)
        s3.upload_file(str(path), bucket, key)
        count += 1
    return count
