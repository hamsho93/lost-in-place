"""Claim a free parallel slot (PX4 instance number) on this host.

Every slot gets its own MAVLink, MAVSDK and Gazebo partition ports (see `sim.py`), so two episodes
on one host must not share a slot. AWS Batch runs EC2 jobs with host networking, so containers on
one instance see each other's ports but not each other's files. A slot is therefore held by
binding an abstract Unix socket: those names are scoped to the network namespace, so they are
shared exactly when the ports are, and the kernel releases them when the process exits, even
after a crash.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager

MAX_SLOTS = 32


def _name(slot: int) -> str:
    return f"\0lost-in-place-slot-{slot}"


@contextmanager
def claim_slot(requested: int | None = None, max_slots: int = MAX_SLOTS) -> Iterator[int]:
    """Hold slot `requested`, or the lowest free one when it is None, until the block exits."""
    candidates = [requested] if requested is not None else list(range(max_slots))
    for slot in candidates:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(_name(slot))
        except OSError:
            sock.close()
            continue
        try:
            yield slot
        finally:
            sock.close()
        return
    if requested is not None:
        raise RuntimeError(f"slot {requested} is already in use on this host")
    raise RuntimeError(f"all {max_slots} slots are in use on this host")
