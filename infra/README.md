# AWS infrastructure (CDK)

One stack, `LostInPlaceSim`, in us-east-1. It contains:

- **Spot AWS Batch.** x86 instances (c7i, c7a, m7i, c6i), 2 vCPU and 3 GB per episode, up to `maxVcpus`. Jobs retry automatically when a spot instance is reclaimed.
- **ECR repository** for the image built from `docker/Dockerfile`.
- **S3 bucket** for results: private, encrypted, TLS only. Runs move to Glacier Instant Retrieval after 30 days.
- **Cost guard:**
  - a monthly AWS Budget with alerts at 50, 80 and 100%;
  - a budget action that blocks job submission through the operator role at 100%;
  - a CloudWatch billing alarm at 120% as a backstop.
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

- Enable *Receive Billing Alerts* in the Billing console, so the billing alarm has data.
- Check the EC2 quota *All Standard Spot Instance Requests* in us-east-1. It must be at least `maxVcpus`.
- Confirm the SNS email subscription.

## Push the image and run episodes

```bash
repo=$(aws cloudformation describe-stacks --stack-name LostInPlaceSim \
  --query "Stacks[0].Outputs[?OutputKey=='ImageRepository'].OutputValue" --output text)
aws ecr get-login-password | docker login --username AWS --password-stdin "${repo%/*}"
(cd .. && docker build -f docker/Dockerfile -t "$repo:latest" .) && docker push "$repo:latest"

# 20 seeds of one scenario (seed = 100 + array index)
aws batch submit-job --job-name lowtex --job-queue <JobQueue> --job-definition <JobDefinition> \
  --array-properties size=20 --parameters scenario=scenarios/fixtures/lowtex_hover.yaml,seed=100
```

Each episode writes `runs/<scenario>_s<seed>/` (ULog, `episode.json`, `metrics.json`) to the results bucket.

## Settings

| Context key | Default | Meaning |
|---|---|---|
| `budgetUsd` | 100 | Monthly budget for the account (all services) |
| `maxVcpus` | 32 | Hard cap on concurrent compute (16 episodes) |
| `alertEmail` | none | Where budget alerts go |
| `imageTag` | latest | Image tag the job definition runs |
| `region` | us-east-1 | Billing metrics only exist in us-east-1 |

## Test

```bash
uv run pytest -q          # assertions on the synthesized template
npx aws-cdk@2 synth       # full CloudFormation output
```
