import argparse
import subprocess
import sys

import pytest

from lost_in_place.cli import _instance_arg
from lost_in_place.slots import claim_slot


def test_concurrent_claims_get_distinct_slots() -> None:
    with claim_slot(max_slots=4) as a, claim_slot(max_slots=4) as b:
        assert a != b


def test_slot_is_free_again_after_release() -> None:
    with claim_slot(max_slots=4) as first:
        pass
    with claim_slot(max_slots=4) as again:
        assert again == first


def test_requested_slot_in_use_is_an_error() -> None:
    with claim_slot(max_slots=4) as held, pytest.raises(RuntimeError, match="in use"), claim_slot(held):
        pass


def test_all_slots_taken_is_an_error() -> None:
    with claim_slot(max_slots=1), pytest.raises(RuntimeError, match="all 1 slots"), claim_slot(max_slots=1):
        pass


def test_slot_held_by_another_process_is_skipped() -> None:
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from lost_in_place.slots import claim_slot\n"
            "with claim_slot(max_slots=4) as s:\n"
            "    print(s, flush=True); sys.stdin.read()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        held = int(holder.stdout.readline())
        with claim_slot(max_slots=4) as mine:
            assert mine != held
    finally:
        holder.communicate("")
    with claim_slot(held, max_slots=4) as reclaimed:
        assert reclaimed == held


@pytest.mark.parametrize(("value", "expected"), [("auto", None), ("0", 0), ("3", 3)])
def test_instance_arg(value: str, expected: int | None) -> None:
    assert _instance_arg(value) == expected


@pytest.mark.parametrize("value", ["-1", "two", ""])
def test_instance_arg_rejects(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _instance_arg(value)
