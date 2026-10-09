import pytest

from lost_in_place.storage import parse_s3_uri


def test_parse_s3_uri() -> None:
    assert parse_s3_uri("s3://bucket/runs/batch1/") == ("bucket", "runs/batch1")
    assert parse_s3_uri("s3://bucket") == ("bucket", "")


@pytest.mark.parametrize("uri", ["bucket/runs", "https://bucket/runs", "s3:///runs"])
def test_parse_s3_uri_rejects_non_s3(uri: str) -> None:
    with pytest.raises(ValueError):
        parse_s3_uri(uri)
