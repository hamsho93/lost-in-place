---
name: run-sweep
description: >-
  Run a benchmark sweep on spot AWS Batch end to end: pinned image, stack deploy, canary,
  manifest, array submission, monitoring, cancelling a bad job, resubmitting, and recording the
  run. Use when asked to run, resume or re-run episodes at scale, or to deploy the sweep stack.
---

# Running a sweep on AWS Batch

Read `infra/AGENTS.md` first. The account is shared, so its rules come before anything here. The steps below are what the E1 pilot (`docs/pilot.md`) needed, in order.

## 1. Before submitting

- **Merged code only.** Deploy and build from `main` after the PRs are merged (`infra/AGENTS.md`). The E1 pilot broke this rule: the stack ran the unmerged sweep job definition with a branch image. A pilot can live with that; a counted sweep can't.
- **`make check` passes.**
- **Pin the image.** Take the digest the `image` workflow pushed from `main`, and deploy it as `imageTag=sha256:...`. Never run counted episodes on `latest`, which is rebuilt every Monday.
- **Spot quota.** `aws service-quotas get-service-quota --service-code ec2 --quota-code L-34B43A08`. Batch can't exceed it, whatever `maxVcpus` says: 2 vCPU per episode, so a quota of 32 runs 16 episodes at once.
- **Mask the account ID** in anything you paste into a PR, a doc or chat: pipe AWS output through `sed -E 's/[0-9]{12}/<account>/g'`.

## 2. Deploy

```bash
cd infra
npx -y aws-cdk@2 diff LostInPlaceSim --no-change-set -c alertEmail=<owner's alert email> -c budgetUsd=450 -c maxVcpus=<n> -c imageTag=sha256:<digest> -c useast2Profiles="$east2"
npx -y aws-cdk@2 deploy LostInPlaceSim <same -c flags> --require-approval never
```

Pass every context key on every deploy. CDK doesn't remember them, so a deploy without `alertEmail` deletes the budget alert's email subscription, and one without `useast2Profiles` (built as in `infra/README.md`, "Models in another region") drops the Sonnet 5.5 and Haiku 5.5 grants. Say in the PR or doc exactly what the diff changed.

## 3. Canary on Batch, not locally

Local runs don't reproduce Batch timing. Locally, `--cpus=2` gives about 0.37× real time and `--cpus=1` stalls boot, while Batch hosts run at 0.16 to 0.34×. The heartbeat failure in `docs/pilot.md` only appeared on Batch. So after any change to the runner, the image or the PX4 setup, submit the first 10 entries of the manifest as their own array (`size=10`, `offset=0`) and look at them with the `review-sweep` skill before submitting the rest.

## 4. Submit

```bash
bucket=$(aws cloudformation describe-stacks --stack-name LostInPlaceSim \
  --query "Stacks[0].Outputs[?OutputKey=='ResultsBucket'].OutputValue" --output text)
uv run lip manifest sweeps/<name>.yaml --upload s3://$bucket/manifests   # prints the submit-job commands
```

Run the printed `aws batch submit-job` commands, filling in `JobQueue` and `SweepJobDefinition` from the stack outputs. Submit through `OperatorRole` when your principal requires it; never grant `batch:SubmitJob` anywhere else, or the budget stop is bypassed.

## 5. Monitor

```bash
aws batch describe-jobs --jobs <array job id> --query 'jobs[0].arrayProperties.statusSummary'
aws batch list-jobs --array-job-id <array job id> --job-status FAILED   # don't also pass --job-queue
uv run lip report s3://$bucket/runs --manifest s3://$bucket/manifests/<name>.json
```

- Poll every few minutes with short waits. A long blocking sleep gets cut off when a message arrives.
- **Stop early if harness failures are high.** If more than about 10% of the first 30 attempts are harness failures, cancel and use the `triage-harness-failures` skill. The pilot's first image failed 34% of attempts, and every failure used up one of an episode's three tries.

## 6. Cancelling a bad job

`cancel-job` only removes queued children. Running children need `terminate-job`:

```bash
for s in SUBMITTED PENDING RUNNABLE STARTING RUNNING; do
  aws batch list-jobs --array-job-id <id> --job-status $s --query 'jobSummaryList[].jobId' --output text
done | tr '\t' '\n' | xargs -r -I{} aws batch terminate-job --job-id {} --reason "<why>"
```

If the job's failures came from a broken image or runner, rather than ordinary harness flakiness, **quarantine its attempts**. Otherwise they count towards each episode's three tries and turn episodes into "missing":

```bash
aws s3 mv s3://$bucket/runs/ s3://$bucket/quarantine/<job id>/ --recursive \
  --exclude '*' --include '*/<job id>-*attempt*'
```

Never quarantine results (non-harness outcomes). The attempt rule forbids re-rolling them (`docs/fidelity.md#which-attempt-counts`).

## 7. Resume

Resubmit the same manifest and offsets. Results live under `runs/<sweep name>/`, so the same name resumes, and a new name (a smoke test of a new image, a confirmatory re-run) flies every episode afresh. `lip run-manifest` skips episodes that already have a result or three harness failures, so only open episodes fly. `lip report --manifest` counts `not_started` and `missing` separately; a sweep is finished when `not_started` is 0.

## 8. Record

Add the run to `docs/`. For a pilot, write a page like `docs/pilot.md`; for fidelity checks, add an entry to `docs/fidelity.md`. Include:

- array job IDs and what happened to each (finished, cancelled, quarantined)
- the image digest and git SHA, and whether results mix images
- the `lip report` table, with harness failures per image and their causes
- what the result means for the experiment design, and what it can't support

Then send the numbers to the coordinator.
