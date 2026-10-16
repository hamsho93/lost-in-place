"""The import warm-up pinger (S-5, S-6), with a dead-man switch on the submitter's heartbeat.

A Custom Model Import scales to zero; its first call after that fails with ModelNotReady while a copy
loads, and the copy is billed per minute from then on. The pinger sends a one-token call every
`interval_s` while the submitter's heartbeat asks for it (`imports.<name>.ping`), and records the
warm-up in `warm/<import>.json`: when it started (the arm's metering window opens there), its first
success (S-5: the arm may submit) or its failure after `warm_timeout_s`.

It stops pinging, so the copy can scale back down, whenever the heartbeat is older than `deadman_s`
or says the submitter stopped; it exits once the import's arms are done.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from lost_in_place.wave.aws import mask, utcnow
from lost_in_place.wave.config import Rules
from lost_in_place.wave.ledger import Ledger, iso, parse_iso

Exit = Literal["done", "deadman", "failed", "max-iterations"]
PING_BODY = {"prompt": "ping", "max_tokens": 1}


def heartbeat_alive(heartbeat: dict[str, Any] | None, now: datetime, rules: Rules) -> str | None:
    """Why the dead-man switch is open, or None while the submitter is alive and running."""
    if heartbeat is None:
        return "no submitter heartbeat"
    age = now - parse_iso(str(heartbeat["at"]))
    if age > timedelta(seconds=rules.deadman_s):
        return f"submitter heartbeat is {int(age.total_seconds())} s old"
    if heartbeat.get("state") == "stopped":
        return "submitter is stopped"
    return None


def invoke_ping(runtime: Any, arn: str) -> None:
    response = runtime.invoke_model(
        modelId=arn, body=json.dumps(PING_BODY), accept="application/json", contentType="application/json"
    )
    response["body"].read()


class Pinger:
    def __init__(
        self,
        ledger: Ledger,
        import_name: str,
        arn: Callable[[], str | None],
        rules: Rules,
        *,
        invoke: Callable[[str], None] | None = None,
        dry_run: bool = False,
        interval_s: float = 30.0,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        out: Callable[[str], None] = print,
    ) -> None:
        self.ledger = ledger
        self.import_name = import_name
        self.arn = arn
        self.rules = rules
        self.invoke = invoke
        self.dry_run = dry_run
        self.interval_s = interval_s
        self.clock = clock
        self.sleep = sleep
        self.out = out

    def restart(self) -> None:
        """Forget a failed warm-up so the next ping starts a new one (S-5: retry later)."""
        record = self.ledger.warm(self.import_name) or {}
        if record.get("failed_at") and not record.get("first_success_at"):
            self.ledger.save_warm(self.import_name, {"previous": record})

    def step(self) -> Exit | None:
        """One iteration: an exit reason, or None to keep going."""
        now = self.clock()
        heartbeat = self.ledger.read_heartbeat()
        dead = heartbeat_alive(heartbeat, now, self.rules)
        if dead:
            self.out(f"dead-man: {dead}; not pinging {self.import_name}")
            return "deadman"
        assert heartbeat is not None
        flags = (heartbeat.get("imports") or {}).get(self.import_name) or {}
        if flags.get("done"):
            self.out(f"{self.import_name}: its arms are done")
            return "done"
        if not flags.get("ping"):
            return None
        record = self.ledger.warm(self.import_name) or {}
        if record.get("failed_at") and not record.get("first_success_at"):
            return "failed"
        if not record.get("started_at"):
            record["started_at"] = iso(now)
        if not record.get("first_success_at") and now - parse_iso(record["started_at"]) > timedelta(
            seconds=self.rules.warm_timeout_s
        ):
            record["failed_at"] = iso(now)
            self.ledger.save_warm(self.import_name, record)
            self.ledger.event(now, "warm_failed", import_name=self.import_name, rule="S-5")
            self.out(f"S-5: {self.import_name} did not answer within {self.rules.warm_timeout_s} s")
            return "failed"
        arn = self.arn()
        if arn is None:
            record["last_error"] = "import not found"
        elif self.dry_run or self.invoke is None:
            self.out(f"dry run: would ping {self.import_name}")
        else:
            record["arn"] = arn
            try:
                self.invoke(arn)
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") or type(exc).__name__
                record["last_error"] = mask(str(code))
            else:
                record.pop("last_error", None)
                record["last_ping_at"] = iso(now)
                if not record.get("first_success_at"):
                    record["first_success_at"] = iso(now)
                    self.ledger.event(now, "warm", import_name=self.import_name)
                    self.out(f"{self.import_name} is warm")
        self.ledger.save_warm(self.import_name, record)
        return None

    def run(self, max_iterations: int | None = None) -> Exit:
        n = 0
        while max_iterations is None or n < max_iterations:
            n += 1
            reason = self.step()
            if reason is not None:
                return reason
            self.sleep(self.interval_s)
        return "max-iterations"
