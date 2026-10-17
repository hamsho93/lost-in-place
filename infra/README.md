# AWS infrastructure (CDK)

One stack, `LostInPlaceSim`, in us-east-1. It contains:

- **Spot AWS Batch.** Intel x86 instances (c7i, m7i, c6i; `xlarge` and `2xlarge` only, so two to four episodes share a host and no episode starts on a fresh 2-vCPU instance; AMD `c7a` is excluded, see [fidelity notes](../docs/fidelity.md)), 2 vCPU and 3 GB per episode, up to `maxVcpus`. A job gets up to 3 attempts. Batch retries when a spot instance is reclaimed, and also when the episode exits non-zero, because Batch retries whenever no `evaluateOnExit` rule matches.
- **ECR repository** for the image built from `docker/Dockerfile`. It keeps the 10 newest images, plus every image with a `freeze-` tag (up to 100), so a frozen design's image stays pullable however many builds follow it. A `docker buildx` push with attestations lists as three images (index, image, attestation), so it uses up the ten faster than the `image` workflow's plain push.
- **S3 bucket** for results: private, encrypted, TLS only. Runs move to Glacier Instant Retrieval after 30 days.
- **Cost guard:**
  - a monthly AWS Budget that counts only resources tagged `project=lost-in-place`, with alerts at 50, 80 and 100% of actual spend and 100% of forecast spend;
  - a budget action that, at 100%, blocks job submission and planner model calls from the operator and job roles;
  - optionally, a CloudWatch alarm on the whole account's estimated charges (`accountAlarmUsd`). It is off by default, because billing metrics can't be filtered by tag.
- **Bedrock planner models.** For each Bedrock model in [`src/lost_in_place/llm/models.yaml`](../src/lost_in_place/llm/models.yaml), an application inference profile tagged `project=lost-in-place`, so model spend counts against the budget. Jobs get the mapping in `LIP_BEDROCK_PROFILES`, and the `BedrockProfiles` output has it for use elsewhere. The roles can call the models only through these profiles. A model whose registry `region` isn't the stack's gets its profile outside the stack (see [Models in another region](#models-in-another-region)).
- **Imported planner models.** If the registry has a `bedrock_imported` model, the job and operator roles may call any model imported into the account with Bedrock Custom Model Import. Importing one is a manual step (see [Imported models](#imported-models)).
- **Operator role** for submitting jobs, reading results and calling the planner models.

There is no NAT gateway: instances sit in public subnets and only make outbound connections.

## Deploy

```bash
cd infra
uv sync
npx aws-cdk@2 bootstrap                                  # once per account/region
npx aws-cdk@2 deploy LostInPlaceSim -c alertEmail=you@example.com -c budgetUsd=450 -c maxVcpus=128
```

Before the first deploy:

- Activate `project` as a user-defined cost allocation tag in the Billing console. Until it is active, the budget sees $0, so neither the alerts nor the stop action fire.
- If you set `accountAlarmUsd`, enable *Receive Billing Alerts* in the Billing console, so the alarm has data.
- Check the EC2 quota *All Standard Spot Instance Requests* in us-east-1. Batch can't go past it, whatever `maxVcpus` says (`aws service-quotas get-service-quota --service-code ec2 --quota-code L-34B43A08`). New accounts start at 32.
- Confirm the SNS email subscription.
- For each Bedrock planner model, enable model access in the Bedrock console (Anthropic models need the use-case form once per account). Without it, calls fail with an access error. "Not available for this account" is different: the account has no agreement for that model, which the console can't fix (see [docs/planners.md](../docs/planners.md#adding-a-model)).

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

## Sweeps

A sweep spec (see [`sweeps/`](../sweeps)) crosses scenarios, reps, planners, validators and observation levels. `lip manifest` expands it into one shuffled entry per episode, with seeds from `hash(pair, rep)`, so a fault scenario and its healthy control fly the same spawn and every arm sees the same seeds. It then prints the `submit-job` commands; Batch arrays hold at most 10,000 children, so larger sweeps get one array per 10,000-entry offset.

```bash
bucket=$(aws cloudformation describe-stacks --stack-name LostInPlaceSim \
  --query "Stacks[0].Outputs[?OutputKey=='ResultsBucket'].OutputValue" --output text)
uv run lip manifest sweeps/e1-pilot.yaml --upload s3://$bucket/manifests
aws batch submit-job --job-name e1-pilot-0 --job-queue <JobQueue> --job-definition <SweepJobDefinition> \
  --array-properties size=270 --parameters manifest=s3://$bucket/manifests/e1-pilot.json,offset=0
uv run lip report s3://$bucket/runs --manifest s3://$bucket/manifests/e1-pilot.json  # per-cell rates, Wilson 95% CIs
```

Each child runs `lip run-manifest`, which uploads to `runs/<sweep name>/<scenario>/<planner>-<validator>-<obs>/r<rep>-s<seed>/<job id>-attempt<n>/`. The sweep name keeps sweeps apart, since episode keys don't name the sweep; give a sweep a new name to fly it again (on a new image, say), and keep the name to resume it. `lip report --manifest` adds the prefix to an `s3://` root. Manifests built before the prefix existed (the pilots) have none, and their results stay under `runs/`. Before flying, it lists that episode's earlier attempts and exits without flying if one of them is a result, or if three attempts have all been harness failures (the episode then counts as missing). Resubmitting the same array after a cancellation or a quota stall therefore only flies the episodes that are still open. `lip report` takes each episode's first attempt that is a result (not a harness failure, or a harness failure whose score shows a violation; see [docs/fidelity.md](../docs/fidelity.md#which-attempt-counts)), and reports the harness-failure rate, missing episodes, and the intra-scenario correlation of violations per cell next to the rates.

An episode exits non-zero only on a harness failure, and not then if its score shows a violation before it. A metrics or scoring error is printed and the episode still uploads and exits zero, because a Batch retry would re-roll a result; re-score it with `lip analyze`.

## Imported models

Open-weight planner models (the Qwen3-4B arms) run on [Bedrock Custom Model Import](https://docs.aws.amazon.com/bedrock/latest/userguide/model-customization-import-model.html), which serves them without a GPU in the account. Bedrock decides the size of a copy at import. Qwen3-4B needs 2 Custom Model Units (CMUs), which cost $0.05718 per CMU per minute in us-east-1: about $6.86 an hour while a copy is active, billed in 5-minute windows. A copy shuts down after 5 minutes without calls, and storage costs $1.95 per CMU per month, so import before a sweep and delete afterwards.

**Once per account**, outside the stack:

- Create a role `lip-bedrock-model-import`. Its trust policy lets `bedrock.amazonaws.com` assume it, and its inline policy allows `s3:GetObject` on `<results bucket>/models/*` and `s3:ListBucket` on the bucket.
- Give whoever imports the right to:
  - create and read import jobs;
  - get, list and delete imported models;
  - tag them (`bedrock:TagResource`);
  - pass that role to `bedrock.amazonaws.com`.
- Stage the weights: the model's Hugging Face files at a pinned revision, under `models/<model>/<revision>/` in the results bucket.

```bash
P=models/qwen3-4b-thinking-2507/768f209     # Qwen/Qwen3-4B-Thinking-2507 at 768f209d9ea8...
for f in config.json generation_config.json merges.txt model.safetensors.index.json tokenizer.json \
    tokenizer_config.json vocab.json LICENSE model-0000{1,2,3}-of-00003.safetensors; do
  curl -sfL "https://huggingface.co/Qwen/Qwen3-4B-Thinking-2507/resolve/768f209d9ea81521153ed38c47d515654e938aea/$f" -o "$f"
done
aws s3 cp --recursive . "s3://$bucket/$P/"
```

The student arm's weights are private: they're staged under `models/qwen3-4b-c1/bedrock-c6bc0a6/`. That directory holds the fused model's `config.json`, `generation_config.json` and safetensors, plus `tokenizer.json`, `tokenizer_config.json`, `vocab.json` and `merges.txt` from `Qwen/Qwen3-4B-Instruct-2507` at the revision in `models.yaml`. The base tokenizer encodes every prompt identically to the student's own. But the student's `tokenizer.json`, as MLX saved it, marks `<tool_call>` as a special token, so a server that skips special tokens would strip the tool call from every reply.

**Before a sweep** that flies an imported arm, import it under the name its `model_id` gives in `models.yaml`. This takes about 11 minutes and costs nothing:

```bash
aws bedrock create-model-import-job --job-name "lip-qwen3-4b-thinking-$(date +%s)" \
  --imported-model-name lip-qwen3-4b-thinking-2507-768f209 \
  --role-arn "arn:aws:iam::<account>:role/lip-bedrock-model-import" \
  --model-data-source "s3DataSource={s3Uri=s3://$bucket/$P/}" \
  --imported-model-tags key=project,value=lost-in-place
aws bedrock get-imported-model --model-identifier lip-qwen3-4b-thinking-2507-768f209 \
  --query customModelUnits                               # once the job's status is Completed
uv run lip propose qwen3-4b-zero-shot                     # one real call; the first waits for a copy
```

Bedrock removes an import's copies after about 5 idle minutes, and bringing one back depends on its on-demand fleet: 1.5 minutes on the evening of Oct 13, 2026, and about 27 minutes of steady retries a few hours later, which failed every attempt of a 10-episode canary. A planner call now waits up to 30 minutes for a copy, with the sim paused, before the episode fails as a harness failure. The wait counts against the episode's wall budget (900 s plus 75 s per planner call), which the long canary episodes used less than half of. Still, warm the model with `lip propose` just before submitting, and keep it warm (a call every 2 minutes) until the first episodes are running, because Batch takes several minutes to start hosts.

The tag is the only way to put an imported model's spend under the project budget: application inference profiles, which the stack uses for foundation models, can only copy a foundation model or a system profile. Whether Bedrock carries the model's tag onto its Custom Model Unit charges isn't documented, so check Cost Explorer, filtered by `project=lost-in-place`, the day after the first canary. If the charges aren't there, the budget can't see this spend: keep `budgetUsd` as is, and watch the sweep with `accountAlarmUsd` instead. The budget stop blocks calls to imported models either way, once it fires.

**After the sweep:** `aws bedrock delete-imported-model --model-identifier lip-qwen3-4b-thinking-2507-768f209`.

The smoke test of Oct 13, 2026 ran DEXI's C0 missions through this path: 8 of 20 seen missions passed, against arm 2's 38% on Ollama. With 16 missions in flight, a single copy kept each call above 100 tokens/s, about 1,100 tokens/s in total. The student (`qwen3-4b-student`, also 2 CMUs) then flew all 100 of DEXI's frozen missions in under 4 minutes, with a median call of 0.35 s once warm. It passed 35 of 50 seen and 28 of 50 unseen missions, against arm 3's 35 and 27 on `mlx_lm.server`, with the same per-template counts except one more unseen coverage pass.

## Models in another region

GPT-5.6 Sol, Claude Sonnet 5.5 and Haiku 5.5 have quota only in us-east-2 (10M, 6M and 10M tokens per minute for their `us.` profiles; 0 in us-east-1), so their registry entries say `region: us-east-2` and the client calls the us-east-2 endpoint. A profile has to live in the region it's called in, and CloudFormation creates resources only in its stack's region. So the stack makes no profile for such an entry. Copying from the us-east-1 system profile would be the wrong one, with no quota.

Instead, each of these profiles is created once, by CLI, in its region and with the project tag. Its ARN is then passed as `-c useast2Profiles='{"<model id>": "<ARN>", ...}'`. With the ARN, the stack:

- lets both roles invoke the profile;
- lets both roles invoke `foundation-model/<model>*` (any region) only under `bedrock:InferenceProfileArn` equal to one of the project's profiles;
- for an OpenAI model, lets both roles invoke the account's `project/default` in that region, which Bedrock also requires for GPT-5.6 Sol on bedrock-runtime;
- adds the ARN to `LIP_BEDROCK_PROFILES` and `BedrockProfiles`.

Synth fails unless the ARN is an `application-inference-profile` of this account in the entry's region, so a system profile can't slip in. An entry without an ARN gets a synth warning and no grant: the roles can't call it at all.

**Create the profiles** once. It costs nothing, and needs `bedrock:CreateInferenceProfile` and `bedrock:TagResource` in us-east-2: the account administrator has both, and `drone`, the operator role and the CDK deploy role have neither.

```bash
acc=$(aws sts get-caller-identity --query Account --output text)
for m in claude-sonnet-5-5 claude-haiku-5-5; do
  aws bedrock create-inference-profile --region us-east-2 \
    --inference-profile-name "LostInPlaceSim-anthropic-$m" \
    --description "lost-in-place planner calls to us.anthropic.$m" \
    --model-source copyFrom="arn:aws:bedrock:us-east-2:$acc:inference-profile/us.anthropic.$m" \
    --tags key=project,value=lost-in-place \
    --query '[inferenceProfileArn,status]' --output text | sed "s/$acc/<account>/g"
done
aws bedrock create-inference-profile --region us-east-2 \
  --inference-profile-name LostInPlaceSim-openai-gpt-5-6-sol \
  --description "lost-in-place planner calls to us.openai.gpt-5.6-sol" \
  --model-source copyFrom="arn:aws:bedrock:us-east-2:$acc:inference-profile/us.openai.gpt-5.6-sol" \
  --tags key=project,value=lost-in-place \
  --query '[inferenceProfileArn,status]' --output text | sed "s/$acc/<account>/g"
```

All three exist (Oct 16): Sonnet 5.5 `adjhqtlbtyxw`, Haiku 5.5 `ay8horiiyywr`, GPT-5.6 Sol `1v6bj762kok7`.

**Read them back** wherever the ARNs are needed. This uses `bedrock:ListInferenceProfiles`, `GetInferenceProfile` and `ListTagsForResource` in us-east-2:

```bash
east2_arn() { aws bedrock list-inference-profiles --region us-east-2 --type-equals APPLICATION \
  --query "inferenceProfileSummaries[?inferenceProfileName=='LostInPlaceSim-$1'].inferenceProfileArn" \
  --output text; }
east2=$(printf '{"us.anthropic.claude-sonnet-5-5":"%s","us.anthropic.claude-haiku-5-5":"%s","us.openai.gpt-5.6-sol":"%s"}' \
  "$(east2_arn anthropic-claude-sonnet-5-5)" "$(east2_arn anthropic-claude-haiku-5-5)" "$(east2_arn openai-gpt-5-6-sol)")
for arn in $(east2_arn anthropic-claude-sonnet-5-5) $(east2_arn anthropic-claude-haiku-5-5) $(east2_arn openai-gpt-5-6-sol); do
  aws bedrock get-inference-profile --region us-east-2 --inference-profile-identifier "$arn" \
    --query '[status,models[].modelArn]' --output text
  aws bedrock list-tags-for-resource --region us-east-2 --resource-arn "$arn" --query tags --output text
done
```

Each one should be `ACTIVE`, list only its own foundation model (once per `us.` region), and carry `project lost-in-place`.

**The budget covers them.** Budgets and cost allocation tags are account-wide, not regional, so the tag filter counts the us-east-2 profiles' spend as it does us-east-1's. Check Cost Explorer, filtered by `project=lost-in-place` and region us-east-2, the day after the first call. `BudgetStop` denies `bedrock:InvokeModel*` on `*` with no condition, and IAM is global, so once attached it blocks the us-east-2 endpoint too. The wave submitter's CloudWatch reads are on `*`, so it can meter `AWS/Bedrock` in us-east-2 as well.

**Deleting them** (only after every arm on them has settled) takes `aws bedrock delete-inference-profile --region us-east-2 --inference-profile-identifier <ARN>`. Then deploy without that `useast2Profiles` entry.

**The alternative: a companion stack.** A second stack in us-east-2 holding only the two `AWS::Bedrock::ApplicationInferenceProfile` resources would also work, but it adds a stack on top of the same two resources. It can be deployed without `cdk bootstrap` in us-east-2, but only one way:

- **`LegacyStackSynthesizer`, or the same resources as a hand-written template.** CloudFormation gets the template inline (it's far under 51,200 bytes), and there are no assets. `CliCredentialsStackSynthesizer` doesn't work: it still uploads the template to the bucket `cdk-hnb659fds-assets-<account>-us-east-2`, which only a bootstrap creates.
- **The us-east-1 CDK deploy role.** It would deploy the stack, passing the us-east-1 CloudFormation execution role (IAM roles are global) as the stack's role, the way `LipSmokeAuditFixes` was made. In bootstrap v31, the deploy role's CloudFormation statement is on `*`, and it may pass that execution role, which has AdministratorAccess by default.

The ARNs would still have to reach this stack by context: a cross-region reference needs SSM parameters and a custom resource. The companion stack would also be a second stack to deploy here, against "only `LostInPlaceSim`" in [`AGENTS.md`](AGENTS.md).

## The freeze-v3 deploy

One deploy, from `main`, before the `freeze-v3-pre` tag (WP17 in the freeze-v3 plan). After it, the stack and the compute environment's AMI stay as they are until the wave ends. The one exception is an A-6 harness fix, which repeats steps 3 to 7 with the new image.

The Oct 16 early deploy ran these steps with the freeze-v2 image and GPT-6.1 Sol in us-east-1. The GPT-5.6 Sol change replaces that arm, so the deploy is repeated once from `main` after it merges, and before the tag: steps 3 to 8, with GPT-5.6 Sol's ARN in `$east2`. The list below is what the deploy changes against the stack as it was before Oct 16. Against the live stack, the repeat changes only the GPT items, the `project/default` grant, the profile map and the image digest.

**Steps that need Mahmood's OK, in this order.** The rest are read-only.

1. GPT-5.6 Sol is already subscribed (below); nothing to enable.
2. Step 2: the three us-east-2 profiles exist; create any that is missing.
3. Step 3: build and push the image.
4. Step 4: tag the freeze-v3 and freeze-v2 images `freeze-*`.
5. Step 6: deploy `LostInPlaceSim`.
6. Step 8: update `LipSmokeAuditFixes`.

None of them needs `cdk bootstrap`, in us-east-2 or anywhere else.

**What it changes, and nothing else:**

- **GPT-5.6 Sol from us-east-2,** in place of GPT-6.1 Sol (Oct 16: the account has no GPT-6 access yet). Its entries say `region: us-east-2`, so the stack makes no profile for it; its ARN comes in `useast2Profiles` with the 5.5 models'. The Oct 16 deploy's us-east-1 profile `LostInPlaceSim-openai-gpt-6-1-sol` is **deleted**, since its entry is gone from `models.yaml`.
- **Both roles' grants.** The job and operator roles may invoke the GPT-5.6 Sol profile, and may invoke `foundation-model/openai.gpt-5.6-sol*` (any region, since `us.` routes to us-east-1, us-east-2 and us-west-2) only under `bedrock:InferenceProfileArn` equal to one of the project's profiles. They may also invoke us-east-2's `project/default`, which Bedrock requires for GPT-5.6 Sol. Nothing is granted on the system profile.
- **The same grants for Sonnet 5.5 and Haiku 5.5,** on their us-east-2 profiles from step 2 (WP6's `claude-sonnet-5-5`, `-fb1`, `-fb2` and smoke-only `claude-haiku-5-5` entries). The foundation-model grants are `foundation-model/anthropic.claude-sonnet-5-5*` and `anthropic.claude-haiku-5-5*`, under the same condition. The stack creates no profile for them.
- **`LIP_BEDROCK_PROFILES`** in both job definitions, and the `BedrockProfiles` output, map GPT-5.6 Sol, Sonnet 5.5 and Haiku 5.5 to their us-east-2 profiles, and drop GPT-6.1 Sol.
- **Both job definitions move to the freeze-v3 image digest.** That is the whole of the v3 job definition: the command, resources, retries, timeout and roles stay the same. CloudFormation registers a new revision and deregisters the freeze-v2 one (`SweepEpisode…:19`, image `e879c1ef`). Re-running a freeze-v2 sweep afterwards therefore needs a redeploy with that digest.
- **The ECR lifecycle rule** that keeps `freeze-*` images.
- **Read-only grants on the operator role** for the wave submitter (`lost_in_place.wave`):
  - `cloudwatch:GetMetricData` and `ListMetrics` on `*`, since they have no resource-level permissions;
  - `budgets:ViewBudget` and `DescribeBudgetActionsForBudget` on `budget/LostInPlaceSim-monthly` only;
  - `iam:ListAttachedRolePolicies` on the operator and job roles only;
  - `bedrock:ListImportedModels` on `*`;
  - `bedrock:DeleteImportedModel` on `imported-model/*`, only where the import carries the tag `project=lost-in-place`. Create imports with that tag (see [Imported models](#imported-models)), or the submitter can't delete them;
  - `cloudformation:DescribeStacks` and `DescribeStackResources` on this stack only.

  The submitter's `sts:GetCallerIdentity` needs no permission, and `bedrock:GetImportedModel` was already granted. The job role gets none of these, and no principal other than the operator gains `batch:SubmitJob`.

`budgetUsd` stays at 450, which is already live (Oct 16). The compute environment isn't touched: its AMI shows `UPDATE_AVAILABLE` and stays as it is.

**GPT-5.6 Sol access.** It is sold through AWS Marketplace (product `prod-k5bywj2464j7c`). The first call in the account subscribes it, and that call must come from a principal with `aws-marketplace:Subscribe`, `Unsubscribe` and `ViewSubscriptions`; the stack's roles have none of these, and shouldn't. The account owner made that call through the us-east-2 profile on Oct 16, and it returned without an access or Marketplace error, so the roles can call it now. Its effort goes in `additionalModelRequestFields` as `{"reasoning": {"effort": "low"}}`; a flat `reasoning_effort` is rejected.

### 0. Before starting

- **The image's inputs are merged:** WP1, WP3, WP4, WP5, WP6, WP10 and WP12. The image bakes in `src/` and `scenarios/`, so it needs:
  - their code;
  - the `gpt-5.6-sol` and `claude-sonnet-5-5` (each with fallbacks), `claude-haiku-5-5` and scripted `rule-*` entries in `models.yaml`;
  - both E5 template trees under `scenarios/e5/`.

  The stack reads the same `models.yaml` at synth, so deploy from the commit the image was built from.
- **GPT-5.6 Sol is subscribed,** as above.
- **Credentials can assume `cdk-hnb659fds-deploy-role`** and are an administrator in us-east-2 (for step 2). Use the same ones as the Oct 16 budget deploy; `drone` can't.
- **Nothing is in flight** on the job queue, so no sweep mixes images.

### 1. Pre-flight (S-0, read-only)

```bash
export AWS_REGION=us-east-1
acc=$(aws sts get-caller-identity --query Account --output text)
mask() { sed "s/$acc/<account>/g"; }
out() { aws cloudformation describe-stacks --stack-name LostInPlaceSim \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
b=(--account-id "$acc" --budget-name LostInPlaceSim-monthly)
aws budgets describe-budget-actions-for-budget "${b[@]}" --query 'Actions[].[ActionId,Status]' --output text
aws budgets describe-budget-action-histories "${b[@]}" --action-id <ActionId> \
  --query 'ActionHistories[].EventType' --output text
aws budgets describe-budget "${b[@]}" \
  --query 'Budget.[BudgetLimit.Amount,CalculatedSpend.ActualSpend.Amount]' --output text
aws bedrock list-imported-models --query 'modelSummaries[].modelName' --output text
for s in SUBMITTED PENDING RUNNABLE STARTING RUNNING; do
  aws batch list-jobs --job-queue "$(out JobQueue)" --job-status $s --query 'jobSummaryList[].jobName' --output text
done
```

Don't start unless:

- the action is `STANDBY`, and its history has no `EXECUTE_ACTION` event, so `BudgetStop` was never attached;
  - with IAM read rights, `aws iam list-entities-for-policy --policy-arn <BudgetStop ARN>` also lists no role;
- the limit is `450.0`;
- there are no imported models;
- no jobs are listed.

If the action isn't `STANDBY`, the stop has fired and its deny is still attached. The account owner reverses it in the Budgets console before anything else.

### 2. Create the us-east-2 profiles

Run the create and read-back commands in [Models in another region](#models-in-another-region). Keep `$east2` for step 5. If a profile already exists, `create-inference-profile` fails with a conflict, and the read-back finds the existing one; don't create a second.

### 3. Build the image

Run the `image` workflow on `main`: Actions → image → Run workflow, branch `main`. Or use the API:

```bash
curl -fsS -X POST -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/hamsho93/lost-in-place/actions/workflows/image.yml/dispatches -d '{"ref":"main"}'
```

It pushes `latest` and the first 12 characters of the commit it built. Take the full SHA from the run's page.

### 4. Record and protect the digest

```bash
name=$(out ImageRepository); name=${name#*/}
sha12=<first 12 characters of the built commit>
digest=$(aws ecr describe-images --repository-name "$name" --image-ids imageTag=$sha12 \
  --query 'imageDetails[0].imageDigest' --output text)
echo "$digest"
tag_image() {  # <digest> <tag>; through OperatorRole, which may push to the repository
  aws ecr put-image --repository-name "$name" --image-tag "$2" --image-digest "$1" --image-manifest \
    "$(aws ecr batch-get-image --repository-name "$name" --image-ids imageDigest="$1" \
       --query 'images[0].imageManifest' --output text)"
}
tag_image "$digest" freeze-v3-pre
tag_image sha256:e879c1ef7ea236a2814ed96d70ff830a73244763f4ac2be824b416f3864a790d freeze-v2
```

The digest and the full git SHA go in three places:

- the deploy note;
- the `freeze-v3-pre` tag message (M1);
- `lip freeze-record --freeze v3 --image-digest`.

Re-tagging uploads no layers. `--image-digest` makes the call fail if the manifest bytes don't hash to the digest. The `freeze-` tags protect both images once step 6 is live. Until then, the freeze-v2 image expires after seven more images (as of Oct 16), counting step 3's push.

### 5. Diff

```bash
git switch main && git pull && cd infra && uv sync
email=$(aws sns list-subscriptions-by-topic --topic-arn "$(aws cloudformation describe-stack-resource \
  --stack-name LostInPlaceSim --logical-resource-id Alerts91F83244 \
  --query StackResourceDetail.PhysicalResourceId --output text)" \
  --query "Subscriptions[?Protocol=='email'].Endpoint" --output text)
flags=(-c alertEmail="$email" -c budgetUsd=450 -c maxVcpus=128 -c imageTag="$digest" -c useast2Profiles="$east2")
npx -y aws-cdk@2 diff LostInPlaceSim --no-change-set "${flags[@]}" 2>&1 | mask
```

**Expected diff:**

- the changes listed above;
- `Image` on `Episode` and `SweepEpisode`, from `@sha256:e879c1ef…` to `@$digest`;
- one removed resource, the GPT-6.1 Sol profile, and no new one. No profile for a us-east-2 model: their ARNs appear only in the grants, the job definitions' environment and the output;
- a new `project/default` statement (us-east-2) on both roles.

Stop if anything else appears: the budget, the SNS subscription, the compute environment, the VPC, or a replaced profile. Stop also on a `No profile ARN for …` warning. `--no-change-set` keeps the diff read-only. Pass every flag every time: `cdk.json` says `imageTag: latest`, a deploy without `-c alertEmail` deletes the alert subscription, and one without `-c useast2Profiles` drops the 5.5 grants.

### 6. Deploy

```bash
npx -y aws-cdk@2 deploy LostInPlaceSim "${flags[@]}" --require-approval never 2>&1 | mask
```

Deploy only `LostInPlaceSim`: never `--all`, and never `cdk bootstrap`.

### 7. Verify

```bash
aws cloudformation describe-stacks --stack-name LostInPlaceSim --query 'Stacks[0].StackStatus' --output text
aws batch describe-job-definitions --job-definition-name "$(out SweepJobDefinition)" --status ACTIVE \
  --query 'jobDefinitions[].[revision,containerProperties.image]' --output text | mask
out BedrockProfiles | mask
aws ecr describe-images --repository-name "$name" --image-ids imageTag=freeze-v3-pre \
  --query 'imageDetails[0].imageDigest' --output text
aws budgets describe-budget-actions-for-budget "${b[@]}" --query 'Actions[].Status' --output text
```

**Expected:**

- `UPDATE_COMPLETE`;
- one active revision of the sweep job definition, on `$digest`. Record it as `<SweepJobDefinition>:<revision>`;
- four profiles in the output: Sonnet 4.6 in us-east-1, and GPT-5.6 Sol, Sonnet 5.5 and Haiku 5.5 in us-east-2, matching `$east2`;
- the `freeze-v3-pre` tag resolves to `$digest`;
- the action is still `STANDBY`.

The first GPT-5.6 Sol and Sonnet 5.5 calls by the stack's roles are M2's smoke calls (`lip propose gpt-5.6-sol` or `claude-sonnet-5-5` as `OperatorRole`, with `LIP_BEDROCK_PROFILES` from the output). They are paid, and they aren't part of this deploy.

### 8. Point the smoke stack at the new image

`LipSmokeAuditFixes` (the Oct 16 audit-fix smoke test, outside this repository) is a hand-written template with one job definition. It reuses this stack's job role, so step 6 already gave it the new grants. To fly the new models it needs only two changes: the image, and a profile map with all four entries. Its `LIP_BEDROCK_PROFILES` is a literal that today maps Sonnet 4.6 only.

The stack was created by the CDK deploy role, with the us-east-1 CloudFormation execution role as its stack role: the operator role can't register job definitions, and `drone` can't pass the job role. So update it the same way, with a session of `cdk-hnb659fds-deploy-role-<account>-us-east-1`:

```bash
aws cloudformation get-template --stack-name LipSmokeAuditFixes --query TemplateBody --output json > smoke.json
jq --arg image "$(out ImageRepository)@$digest" --arg profiles "$(out BedrockProfiles)" \
  '.Resources.SmokeJobDefinition.Properties.ContainerProperties |=
     (.Image = $image | .Environment = [{Name: "LIP_BEDROCK_PROFILES", Value: $profiles}])' \
  smoke.json > smoke-v3.json
diff <(jq -S . smoke.json) <(jq -S . smoke-v3.json) | mask   # Image and the map, nothing else
cs=(--stack-name LipSmokeAuditFixes --change-set-name freeze-v3)
aws cloudformation create-change-set "${cs[@]}" --template-body file://smoke-v3.json \
  --role-arn "arn:aws:iam::$acc:role/cdk-hnb659fds-cfn-exec-role-$acc-us-east-1"
aws cloudformation wait change-set-create-complete "${cs[@]}"
aws cloudformation describe-change-set "${cs[@]}" \
  --query 'Changes[].ResourceChange.[Action,LogicalResourceId,Replacement]' --output text
aws cloudformation execute-change-set "${cs[@]}"
aws cloudformation wait stack-update-complete --stack-name LipSmokeAuditFixes
aws batch describe-job-definitions --job-definition-name LipSmokeAuditFixes --status ACTIVE \
  --query 'jobDefinitions[].[revision,containerProperties.image]' --output text | mask
```

The change set should show one `Modify` of `SmokeJobDefinition` with `Replacement` `False`. Changing `ContainerProperties` registers a new revision under the same name and deregisters the old one. Only `JobDefinitionName` and `Tags` force a replacement, and replacing a resource with a fixed name fails, so leave both alone. Keep the retry rule, parameters, roles and log group as they are.

### Rollback

- **A failed deploy rolls itself back** (`UPDATE_ROLLBACK_COMPLETE`). Diff again before retrying.
- **To undo the image only,** redeploy `main` with `-c imageTag=sha256:e879c1ef…`.
- **To undo the GPT-5.6 Sol grants,** deploy the same flags with its entry removed from `useast2Profiles`.
- **To undo the us-east-2 grants,** deploy the same flags without `-c useast2Profiles`. The us-east-2 profiles stay, unused and free, until deleted by hand.
- **The smoke stack** goes back the same way, with `smoke.json` as the template.
- **Either way,** CloudFormation registers a new job definition revision; old revisions can't be reactivated.
- **Never delete** the repository, the bucket or the `freeze-` tags.

## Settings

| Context key | Default | Meaning |
|---|---|---|
| `budgetUsd` | 100 | Monthly budget for resources tagged `project=lost-in-place` |
| `accountAlarmUsd` | 0 (off) | Alarm threshold for the whole account's estimated charges, across all projects |
| `maxVcpus` | 128 | Hard cap on concurrent compute (64 episodes), if the Spot quota allows |
| `alertEmail` | none | Where budget alerts go |
| `githubRepo` | `hamsho93@17190997/lost-in-place@1411196677` (cdk.json) | Repository segment of the GitHub OIDC subject whose `main` branch may push images; empty skips the push role |
| `imageTag` | latest | Image tag or digest (`sha256:...`) the job definition runs. Pin a digest for any run whose results count: the `image` workflow rebuilds `latest` every Monday |
| `region` | us-east-1 | Billing metrics only exist in us-east-1 |
| `useast2Profiles` | none | JSON map from Bedrock model id to the tagged profile ARN made for it in its registry region ([Models in another region](#models-in-another-region)). Pass it with `-c` only: it holds the account ID, so it never goes in `cdk.json` |

Untagged costs, such as data transfer and the instances' root EBS volumes, fall outside the budget. They are small next to instance hours.

## Test

```bash
uv run pytest -q          # assertions on the synthesized template
npx aws-cdk@2 synth       # full CloudFormation output
```
