from pathlib import Path

import pytest

from lost_in_place.storage import attempt_label, parse_s3_uri, upload_prefix


def test_parse_s3_uri() -> None:
    assert parse_s3_uri("s3://bucket/runs/batch1/") == ("bucket", "runs/batch1")
    assert parse_s3_uri("s3://bucket") == ("bucket", "")


@pytest.mark.parametrize("uri", ["bucket/runs", "https://bucket/runs", "s3:///runs"])
def test_parse_s3_uri_rejects_non_s3(uri: str) -> None:
    with pytest.raises(ValueError):
        parse_s3_uri(uri)


def test_batch_attempts_get_their_own_label() -> None:
    first = attempt_label({"AWS_BATCH_JOB_ID": "e4c39db1", "AWS_BATCH_JOB_ATTEMPT": "1"})
    retry = attempt_label({"AWS_BATCH_JOB_ID": "e4c39db1", "AWS_BATCH_JOB_ATTEMPT": "2"})
    assert (first, retry) == ("e4c39db1-attempt1", "e4c39db1-attempt2")
    array_child = attempt_label({"AWS_BATCH_JOB_ID": "e4c39db1:3", "AWS_BATCH_JOB_ATTEMPT": "1"})
    assert array_child == "e4c39db1-3-attempt1"


def test_local_runs_are_labelled_by_time() -> None:
    assert attempt_label({}).startswith("local-")


def test_upload_prefix_nests_the_attempt_under_the_episode() -> None:
    assert upload_prefix("runs", Path("runs/flow_off_s1"), "job-attempt2") == "runs/flow_off_s1/job-attempt2"
    assert upload_prefix("", Path("flow_off_s1"), "job-attempt1") == "flow_off_s1/job-attempt1"
