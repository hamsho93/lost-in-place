"""The E5 wave's real-time cost meter (plan section 10.3; logic in `lost_in_place.wave.meter`).

    uv run --with boto3 python scripts/e5_meter.py now
    uv run --with boto3 python scripts/e5_meter.py --as-caller --dry-run validate --sweep e4-claude

`validate` compares a finished sweep's episode-trace tokens and Batch attempt run time with
CloudWatch's token counts and `JobAttemptExecutionDuration` over the same window (read-only).
"""

import sys

from lost_in_place.wave.cli import meter_main

if __name__ == "__main__":
    sys.exit(meter_main())
