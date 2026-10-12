# Agent guide: infra/

One CDK stack, `LostInPlaceSim`, in us-east-1. [README.md](README.md) covers what it contains and how to deploy it. This page covers what to be careful about.

## The AWS account is shared

The account also runs unrelated production stacks. Anything account-wide affects them too.

- Only ever deploy `LostInPlaceSim`. Never deploy, modify or delete other stacks, and never re-run `cdk bootstrap` (it's already at version 31).
- Deploy only from `main`, after the change is merged, and run `cdk diff` against the live stack first. Say in the PR exactly what the diff changes.
- Cost controls must stay scoped to this project. The budget filters on the `project=lost-in-place` tag (`Tags.of(self)` tags everything). The account-wide billing alarm (`accountAlarmUsd`) is off by default; leave it that way.
- The GitHub OIDC provider (`token.actions.githubusercontent.com`) belongs to the account and is shared with other projects. Import it (`from_open_id_connect_provider_arn`); never create or delete it here.
- Keep the account ID out of git (see the root [AGENTS.md](../AGENTS.md)). Print CLI output through a filter that masks it.

## Cost guard

- Jobs are submitted through `OperatorRole`. At 100% of the budget, the `StopAtBudget` action attaches a deny on `batch:SubmitJob` to that role. Never grant `batch:SubmitJob` to any other principal (users, CI roles), or the stop is bypassed.
- `maxVcpus` caps concurrent compute, and the compute environment scales to 0 when idle. Keep `minv_cpus=0`.

- Planner model calls (Bedrock) count against the budget only through the tagged application inference profiles this stack creates. Never grant `bedrock:InvokeModel` on a system inference profile (`us.*`, `global.*`) or on foundation models without the `bedrock:InferenceProfileArn` condition; that spend would be untagged and unbudgeted.
- Models from other providers (e.g. OpenAI) aren't covered by the AWS budget at all. Give them a spending cap on the provider's side before running them at scale.

## Details that have bitten before

- AWS managed policy ARNs vary in path. `AWSBudgetsActionsWithAWSResourceControlAccess` is at `policy/`, not `policy/service-role/`. Check the [AWS managed policy reference](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/) before referencing one.
- The repository was created after 2026-07-15, so GitHub uses immutable OIDC subjects. `githubRepo` in `cdk.json` is `owner@OWNER_ID/name@REPO_ID`, not `owner/name`. Confirm it with `GET /repos/{owner}/{repo}/actions/oidc/customization/sub`.
- `ImagePushRole` trusts only `main`, so pushes from other branches fail by design.
- The ECR repository and the results bucket are `RETAIN`. A failed first deploy leaves them behind, and a redeploy creates new ones under new names. Delete the leftovers by hand.
- Batch EC2 jobs always use host networking. See the slots note in the root [AGENTS.md](../AGENTS.md).
- The `SPOT_INSTANCE_RECLAIMED` retry rule doesn't limit retries to reclaims. When no `evaluateOnExit` rule matches, Batch retries anyway, so a failing episode runs up to 3 times. To stop that, add an `EXIT` rule for `onExitCode: "*"`.
- The results bucket isn't versioned. Uploads must not reuse keys; the runner nests each attempt under `<job id>-attempt<n>/`.

## Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q   # template assertions
npx --yes aws-cdk@2 synth --quiet
```

Each new resource or IAM grant needs a template assertion in `tests/test_sim_stack.py`.
