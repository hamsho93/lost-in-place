"""The wave's ledger under `<ledger_prefix>/` (S3 `manifests/e5/ledger/` for E5).

    jobs/<arm>/<submitted>-<offset>-<n>.json   one per Batch submission; never rewritten
    heartbeat.json                              the submitter's last cycle; the pinger's dead-man switch
    state.json                                  pauses, cap cuts, ramp, gate, windows, stops (resume reads it)
    commands.log                                operator commands (pause, resume, windows, clear-stop)
    warm/<import>.json                          the pinger's warm-up record for one import
    gate/<time>.txt                             the gate command's output
    s4.log                                      one line per classified attempt: result or retry
    events.log                                  submissions, rule triggers, operator actions

Resume reads only this and the results under `runs/`, so the submitter can die and restart anywhere.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from lost_in_place.wave.store import Store


def iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


@dataclass(frozen=True)
class JobRecord:
    arm: str
    job_id: str
    job_name: str
    manifest: str  # bucket-relative key of the manifest the job flies
    offset: int
    size: int  # 1: a single job running entry `offset`; otherwise an array over offset..offset+size-1
    submitted_at: str
    dry_run: bool = False

    @property
    def indices(self) -> range:
        return range(self.offset, self.offset + self.size)

    def child_ids(self) -> list[str]:
        return [self.job_id] if self.size == 1 else [f"{self.job_id}:{k}" for k in range(self.size)]

    def index_of_child(self, child_id: str) -> int:
        return self.offset if self.size == 1 else self.offset + int(child_id.rsplit(":", 1)[1])


@dataclass
class State:
    paused: dict[str, str] = field(default_factory=dict)
    cap_cut: dict[str, int] = field(default_factory=dict)
    last_cap_cut: dict[str, str] = field(default_factory=dict)
    ramp: dict[str, int] = field(default_factory=dict)
    hard_stop: str | None = None
    gate: dict[str, Any] | None = None
    windows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    s7_checked: dict[str, int] = field(default_factory=dict)
    # {month-fit arm: its section 10.4 decision, the month it was made for, and `preview` when it was
    # made ahead of the arm's turn (informational; decided again when the arm becomes startable)}.
    month_fit: dict[str, dict[str, Any]] = field(default_factory=dict)
    kill_pings: dict[str, str] = field(default_factory=dict)
    deleted_imports: dict[str, str] = field(default_factory=dict)
    import_arns: dict[str, str] = field(default_factory=dict)
    compute_s: dict[str, float] = field(default_factory=dict)
    # {arm: {rule: attempts or results seen when the operator resumed it}}: a resumed rule looks only
    # at what came after.
    acked: dict[str, dict[str, int]] = field(default_factory=dict)
    commands_applied: int = 0

    @classmethod
    def from_json(cls, text: bytes | None) -> State:
        if not text:
            return cls()
        data = json.loads(text)
        decisions = data.get("month_fit")
        if decisions is None or "decision" in decisions:
            data.pop("month_fit", None)
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), indent=1, sort_keys=True).encode()


class Ledger:
    def __init__(self, store: Store, prefix: str) -> None:
        self.store = store
        self.prefix = prefix.rstrip("/")
        self._seq = 0

    def key(self, *parts: str) -> str:
        return "/".join([self.prefix, *parts])

    def record_job(self, record: JobRecord) -> None:
        self._seq += 1
        stamp = record.submitted_at.replace(":", "").replace("-", "")
        name = f"{stamp}-{record.offset:05d}-{os.getpid()}-{self._seq}.json"
        self.store.put(self.key("jobs", record.arm, name), json.dumps(asdict(record)).encode())

    def jobs(self, arm: str) -> list[JobRecord]:
        records = []
        for key in self.store.keys(self.key("jobs", arm) + "/"):
            body = self.store.get(key)
            if body:
                records.append(JobRecord(**json.loads(body)))
        return records

    def heartbeat(self, payload: dict[str, Any]) -> None:
        body = {"host": socket.gethostname(), "pid": os.getpid(), **payload}
        self.store.put(self.key("heartbeat.json"), json.dumps(body, indent=1, sort_keys=True).encode())

    def read_heartbeat(self) -> dict[str, Any] | None:
        body = self.store.get(self.key("heartbeat.json"))
        return dict(json.loads(body)) if body else None

    def state(self) -> State:
        return State.from_json(self.store.get(self.key("state.json")))

    def save_state(self, state: State) -> None:
        self.store.put(self.key("state.json"), state.to_json())

    def warm(self, import_name: str) -> dict[str, Any] | None:
        body = self.store.get(self.key("warm", f"{import_name}.json"))
        return dict(json.loads(body)) if body else None

    def save_warm(self, import_name: str, record: dict[str, Any]) -> None:
        self.store.put(self.key("warm", f"{import_name}.json"), json.dumps(record, indent=1).encode())

    def s4(self, lines: list[tuple[str, str, str, str, str]]) -> None:
        """(time, arm, episode key, attempt label, 'result' or 'retry') per line."""
        for *_, verdict in lines:
            if verdict not in ("result", "retry"):
                raise ValueError(f"S4 log takes only 'result' or 'retry', not {verdict!r}")
        if lines:
            self.store.append(self.key("s4.log"), "".join("\t".join(line) + "\n" for line in lines))

    def s4_logged(self) -> set[tuple[str, str]]:
        """(episode key, attempt label) of every attempt already in the S4 log."""
        body = self.store.get(self.key("s4.log")) or b""
        seen = set()
        for line in body.decode().splitlines():
            parts = line.split("\t")
            if len(parts) == 5:
                seen.add((parts[2], parts[3]))
        return seen

    def event(self, when: datetime, kind: str, **fields: Any) -> None:
        line = json.dumps({"at": iso(when), "event": kind, **fields}, sort_keys=True)
        self.store.append(self.key("events.log"), line + "\n")

    def command(self, when: datetime, name: str, **fields: Any) -> None:
        """An operator command for the submitter, applied at its next cycle (it is the only writer of
        state.json, so commands never race with it)."""
        line = json.dumps({"at": iso(when), "command": name, **fields}, sort_keys=True)
        self.store.append(self.key("commands.log"), line + "\n")

    def commands(self) -> list[dict[str, Any]]:
        body = self.store.get(self.key("commands.log")) or b""
        return [json.loads(line) for line in body.decode().splitlines() if line.strip()]

    def gate_output(self, when: datetime, text: str) -> str:
        key = self.key("gate", f"{iso(when).replace(':', '')}.txt")
        self.store.put(key, text.encode())
        return key
