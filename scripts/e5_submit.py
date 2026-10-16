"""The E5 wave's rolling submitter (`docs/runbooks/e5-wave.md`; logic in `lost_in_place.wave`).

uv run --with boto3 python scripts/e5_submit.py preflight
uv run --with boto3 python scripts/e5_submit.py run [--dry-run] [--once]
uv run --with boto3 python scripts/e5_submit.py ping --import <import name>
uv run --with boto3 python scripts/e5_submit.py status | pause ARM | resume ARM | window ARM start|end
"""

import sys

from lost_in_place.wave.cli import submit_main

if __name__ == "__main__":
    sys.exit(submit_main())
