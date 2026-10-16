"""Operating a confirmatory wave on AWS Batch: the rolling submitter, the cost meter, the stop rules,
the import pinger and the blinded health report (freeze-v3 plan section 10; `docs/runbooks/e5-wave.md`).

Nothing here imports boto3 at module level: AWS clients come from `wave.aws.Clients`, so tests pass
fakes and the core package keeps working without the `aws` extra.
"""
