# AWS infrastructure (CDK)

One stack, `LostInPlaceSim`, in us-east-1. It contains:

- **Spot AWS Batch.** x86 instances (c7i, c7a, m7i, c6i), 2 vCPU and 3 GB per episode, up to `maxVcpus`. A job gets up to 3 attempts. Batch retries when a spot instance is reclaimed, and also when the episode exits non-zero, because Batch retries whenever no `evaluateOnExit` rule matches.
- **ECR repository** for the image built from `docker/Dockerfile`.
- **S3 bucket** for results: private, encrypted, TLS only. Runs move to Glacier Instant Retrieval after 30 days.
- **Cost guard:**
  - a monthly AWS Budget that counts only resources tagged `project=lost-in-place`, with alerts at 50, 80 and 100% of actual spend and 100% of forecast spend;
  - a budget action that blocks job submission through the operator role at 100%;
  - optionally, a CloudWatch alarm on the whole account's estimated charges (`accountAlarmUsd`). It is off by default, because billing metrics can't be filtered by tag.
- **Operator role** for submitting jobs and reading results.

There is no NAT gateway: instances sit in public subnets and only make outbound connections.

## Deploy

```bash
cd infra
uv sync
npx aws-cdk@2 bootstrap                                  # once per account/region
npx aws-cdk@2 deploy -c alertEmail=you@example.com -c budgetUsd=100 -c maxVcpus=32
```

Before the first deploy:

- Activate `project` as a user-defined cost allocation tag in the Billing console. Until it is active, the budget sees $0, so neither the alerts nor the stop action fire.
- If you set `accountAlarmUsd`, enable *Receive Billing Alerts* in the Billing console, so the alarm has data.
- Check the EC2 quota *All Standard Spot Instance Requests* in us-east-1. It must be at least `maxVcpus`.
- Confirm the SNS email subscription.

## Push the image and run episodes

The `image` workflow pushes `latest` and the short commit SHA to ECR when it runs on `main`. Set two repository variables from the stack outputs once (under Settings → Secrets and variables → Actions → Variables):

- `AWS_IMAGE_PUSH_ROLE`: the `ImagePushRoleArn` output;
- `ECR_IMAGE_REPOSITORY`: the `ImageRepository` output.

While either variable is unset, the workflow only builds the image and runs `doctor`. The push role uses the account's GitHub OIDC provider (`token.actions.githubusercontent.com`), which must already exist; create it once per account if it does not.

`githubRepo` must match the repository part of the OIDC token's `sub` claim. This repository was created after 2026-07-15, so GitHub uses its immutable format, `owner@OWNER_ID/name@REPO_ID`, rather than `owner/name`. Check it with `gh api repos/OWNER/REPO/actions/oidc/customization/sub` (the `sub_claim_prefix` field, minus the leading `repo:`).

To push by hand instead:

```bash
repo=$(aws cloudformation describe-stacks --stack-name LostInPlaceSim \
  --query "Stacks[0].Outputs[?OutputKey=='ImageRepository'].OutputValue" --output text)
aws ecr get-login-password | docker login --username AWS --password-stdin "${repo%/*}"
(cd .. && docker build -f docker/Dockerfile -t "$repo:latest" .) && docker push "$repo:latest"

# 20 seeds of one scenario (seed = 100 + array index)
aws batch submit-job --job-name lowtex --job-queue <JobQueue> --job-definition <JobDefinition> \
  --array-properties size=20 --parameters scenario=scenarios/fixtures/lowtex_hover.yaml,seed=100
```

Each attempt writes `runs/<scenario>_s<seed>/<job id>-attempt<n>/` to the results bucket: ULog, `episode.json`, `metrics.json`, `px4_console.txt` and `mavsdk_server.txt`. Retries and reruns get their own folder, so a failed attempt is kept next to the retry that succeeded. The bucket isn't versioned, so anything uploaded under the same key is replaced.

## Settings

| Context key | Default | Meaning |
|---|---|---|
| `budgetUsd` | 100 | Monthly budget for resources tagged `project=lost-in-place` |
| `accountAlarmUsd` | 0 (off) | Alarm threshold for the whole account's estimated charges, across all projects |
| `maxVcpus` | 32 | Hard cap on concurrent compute (16 episodes) |
| `alertEmail` | none | Where budget alerts go |
| `githubRepo` | `hamsho93@17190997/lost-in-place@1411196677` (cdk.json) | Repository segment of the GitHub OIDC subject whose `main` branch may push images; empty skips the push role |
| `imageTag` | latest | Image tag the job definition runs |
| `region` | us-east-1 | Billing metrics only exist in us-east-1 |

Untagged costs, such as data transfer and the instances' root EBS volumes, fall outside the budget. They are small next to instance hours.

## Test

```bash
uv run pytest -q          # assertions on the synthesized template
npx aws-cdk@2 synth       # full CloudFormation output
```
