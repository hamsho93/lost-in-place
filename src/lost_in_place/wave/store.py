"""Where the wave's ledger, heartbeat and logs live: S3 for a real wave, a local directory for a dry run.

`Overlay` is the dry-run store: it reads from S3 (what is really there) and writes to a local
directory, so a dry run sees the live ledger but never changes it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol


class Store(Protocol):
    def get(self, key: str) -> bytes | None: ...

    def put(self, key: str, body: bytes) -> None: ...

    def append(self, key: str, text: str) -> None: ...

    def keys(self, prefix: str) -> list[str]: ...

    def where(self, key: str) -> str: ...


def _error_code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


class S3Store:
    """Keys are relative to the bucket. `append` is a conditional read-modify-write (If-Match on the
    object's ETag, If-None-Match for a new one), retried on a conflict, so two writers never lose a line."""

    def __init__(self, s3: Any, bucket: str, retries: int = 8, sleep: Any = time.sleep) -> None:
        self.s3 = s3
        self.bucket = bucket
        self.retries = retries
        self.sleep = sleep

    def where(self, key: str) -> str:
        return f"s3://<results>/{key}"

    def _get(self, key: str) -> tuple[bytes, str] | None:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _error_code(exc) in ("NoSuchKey", "404"):
                return None
            raise
        return bytes(response["Body"].read()), str(response.get("ETag", ""))

    def get(self, key: str) -> bytes | None:
        found = self._get(key)
        return found[0] if found else None

    def put(self, key: str, body: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body)

    def append(self, key: str, text: str) -> None:
        for attempt in range(self.retries):
            found = self._get(key)
            condition = {"IfMatch": found[1]} if found else {"IfNoneMatch": "*"}
            body = (found[0] if found else b"") + text.encode()
            try:
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, **condition)
                return
            except Exception as exc:
                if _error_code(exc) not in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
                    raise
                self.sleep(0.2 * 2**attempt)
        raise RuntimeError(f"could not append to {key} after {self.retries} conflicts")

    def keys(self, prefix: str) -> list[str]:
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
        return sorted(obj["Key"] for page in pages for obj in page.get("Contents", []))


class LocalStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def where(self, key: str) -> str:
        return str(self.root / key)

    def get(self, key: str) -> bytes | None:
        path = self.root / key
        return path.read_bytes() if path.exists() else None

    def put(self, key: str, body: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def append(self, key: str, text: str) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(text)

    def keys(self, prefix: str) -> list[str]:
        if not self.root.exists():
            return []
        found = (p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        return sorted(k for k in found if k.startswith(prefix))


class Overlay:
    """Reads fall through to `base` for keys never written locally; writes stay local."""

    def __init__(self, base: Store, local: LocalStore) -> None:
        self.base = base
        self.local = local

    def where(self, key: str) -> str:
        return self.local.where(key)

    def get(self, key: str) -> bytes | None:
        mine = self.local.get(key)
        return mine if mine is not None else self.base.get(key)

    def put(self, key: str, body: bytes) -> None:
        self.local.put(key, body)

    def append(self, key: str, text: str) -> None:
        if self.local.get(key) is None:
            self.local.put(key, self.base.get(key) or b"")
        self.local.append(key, text)

    def keys(self, prefix: str) -> list[str]:
        return sorted(set(self.base.keys(prefix)) | set(self.local.keys(prefix)))
