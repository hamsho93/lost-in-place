# E5 wave runbook

How to fly the E5 wave (freeze-v3 plan §10.5 and §12 step 5), following the cost and operations review. The submitter, the import pinger, the meter and the health report all run on the operator's machine, in tmux. They read and write AWS through OperatorRole, whose credentials refresh themselves. The bucket, queue, role and inference profiles come from the `LostInPlaceSim` stack's outputs at run time.

Arms, caps, order, prices and thresholds live in `src/lost_in_place/wave/e5.yaml`. The adaptation PR (WP18) fills in the E5 sweep paths, `n_families`, and the canary cost per episode and pinned smoke config of GPT-5.6 Sol and Claude Sonnet 5.5 (the entry each smoke call accepted). Nothing else should change after the `freeze-v3` tag.

```bash
alias e5='uv run --extra aws python scripts/e5_submit.py'
alias meter='uv run --extra aws python scripts/e5_meter.py'
```

## Blinding

During the wave, read only `lip e5 health`, the submitter's output, `e5 status` and the meter. Never open a `score.json` or `episode.json` (an `episode.json` also holds Claude Sonnet 5.5's reasoning summaries, which are blinded like refusal reasons), and don't run `lip stats` or `lip report` on an E5 sweep. Every health query is appended to `manifests/e5/queries.log`, which is disclosed. The S4 log records only `result` or `retry`.

## 1. Pre-flight (S-0)

Run these before the wave starts, and again whenever the submitter is restarted after a stop.

1. `uv run lip freeze-record --freeze v3 --check` passes on the checked-out `freeze-v3` tag.
2. `e5 preflight` must print `S-0: pre-flight passed`. It checks that:
   - the budget's stop action is `STANDBY`;
   - BudgetStop is attached to neither OperatorRole nor JobRole;
   - the meter reads;
   - no imported model exists outside the config, and none has outlived its arms;
   - every arm's sweep exists, and its manifest equals any copy already in S3;
   - the `models.yaml` entries of GPT-5.6 Sol and Claude Sonnet 5.5 equal their pinned smoke configs;
   - every Bedrock arm's `models.yaml` entry exists, and its `region` is the region of the arm's tagged profile in the stack's `BedrockProfiles` (otherwise the meter would read the wrong region);
   - the freeze-record check passes.
3. `e5 --dry-run run --once` goes through one full cycle and makes no AWS writes. Its ledger goes to `.e5-dryrun/`. Check the planned submissions it prints.

The submitter repeats the wave-level checks when it starts, and the per-arm checks just before each arm's first submission. A failure pauses that arm.

## 2. Order and caps

The submitter starts each arm once its conditions hold. It tops the arm up to its in-flight cap every minute, using arrays of at most 10 of the arm's next open manifest entries. The wave as a whole is held to 64 in flight, which is the compute environment's vCPU limit.

| Step (§12) | Arm | Starts when | In flight | S-7 cap (N = 120) |
|---|---|---|---|---|
| 1 | `scripted-gate`: loud O1 and O0, brief, healthy, both cadences | at once | ramps 10, 20, … to 64 | $10 (shared with `scripted-rest`) |
| | S-1 gate check | when `scripted-gate` settles | | |
| 2 | `claude` | gate passed | 25 | $95 |
| 2 | `student` | gate passed and its import's first successful call | 39 | $50 |
| 2 | `zero-shot` | student ≥ 90% settled and its import's first successful call | 39, then 64 when Claude ends | $105 |
| 3 | `scripted-rest`: timed loud, flowscale, misaligned | Claude ≥ 90% and the student settled | 64 | |
| 4 | `gpt-5.6-sol` | every other Batch arm settled, S-8 admitted, §10.4 sum fits | 40 | none; S-8 governs |
| 4 | `claude-sonnet-5-5` (us-east-2) | GPT settled, not admitted or dropped; S-8 admitted; §10.4 sum fits with GPT's spend included | ramps 10, 20, … to 40 | none; S-8 governs |
| 5 | probes: `probe-claude`, `probe-student`, `probe-zero-shot`, `probe-gpt`, `probe-sonnet-5-5` | operator, by window | | $16 in total, plus GPT's and Sonnet's, which count in their arms |
| 6 | `labeller` (outside AWS) | operator, by window | | $32 |

The S-7 caps scale by 1.25 when `n_families` is above 120. S-4 lowers an arm's cap by 5 each time it fires.

Claude Sonnet 5.5 has quota only in us-east-2, so its jobs (still on Batch in us-east-1) call a tagged profile there. us-east-2 lists a 6M tokens-per-minute quota for it, and no requests-per-minute or tokens-per-day quota. A call holds its input plus `max_tokens` (about 2.5k + 4,096 tokens) when it starts and settles at input plus 10 to 15 × its output tokens. At about one call per slot a minute, 40 slots use under 0.3M TPM. So its cap matches GPT's 40, and the ramp and S-4 cover any limit that isn't listed. The cap only changes wall time (about 5 h at 40), not cost.

GPT-5.6 Sol is called the same way, through its own tagged profile in us-east-2, the only region with quota for it. us-east-2 lists 10M tokens a minute for its `us.` profile, with output tokens counting 10× against it. In the frontier smoke a call took about 590 input and 80 output tokens, so 40 slots at about one call a minute use well under 1M TPM. Its calls also need `bedrock:InvokeModel` on the region's `project/default`, which the stack grants.

## 3. Imports and warm-up

Imported models scale to zero. A cold call fails with `ModelNotReady` for 1.5 to 27 minutes, and every minute a copy is up costs about $0.114. The aim is that only one Qwen copy is ever warm with nothing to serve, and for at most 15 minutes.

1. **Day 0, evening:** import both Qwen models, one after the other. Each takes 11 to 13 minutes, and only one import can run at a time. Don't invoke them.
2. **For each import,** start a pinger in its own tmux window, after the submitter:

   ```bash
   e5 ping --import lip-qwen3-4b-c1-c6bc0a6
   e5 ping --import lip-qwen3-4b-thinking-2507-768f209
   ```

   A pinger sends a one-token call every 30 s, but only while the submitter's heartbeat asks for it. That happens when the import's arm is ready to submit and none of its episodes is running yet. The pinger records the warm-up start (the arm's metering window opens there) and the first success in `warm/<import>.json`. The arm is submitted at the next cycle.
3. **Dead-man switch:** a pinger exits as soon as the heartbeat is more than 5 minutes old, or the submitter reports a hard stop. After restarting the submitter, restart its pingers.
4. **S-5:** if a warm-up runs 30 minutes without a success, the pinger records a failure and exits. The arm doesn't submit. Retry later with `e5 ping --import <name> --restart`.
5. **S-6:** if a copy has been up for 10 minutes with none of the import's episodes running, the submitter stops that import's pings. `e5 unkill <import>` lets the pinger warm it again.
6. **Deletion:** when every arm using an import has settled, the submitter deletes the import.

## 4. Submitter, ledger and resume

```bash
tmux new -s e5-wave
e5 run          # pre-flight, then a cycle every 60 s until every Batch arm settles
```

Each cycle:

1. reads the arms' Batch jobs and their attempts under `runs/<sweep>/`;
2. writes the S4 verdicts;
3. applies queued operator commands;
4. checks S-2 to S-9;
5. evaluates the gate when its cells have settled;
6. decides which imports to ping or delete;
7. tops up each startable arm;
8. writes the heartbeat and state.

The ledger is in S3 under `manifests/e5/ledger/`:

| Key | What |
|---|---|
| `jobs/<arm>/<time>-<offset>-<pid>-<n>.json` | one record per submission: job id, manifest, offset, array size |
| `heartbeat.json` | the last cycle: time, `running` or `stopped`, per-arm counts, import ping flags, month estimate |
| `state.json` | pauses, cap cuts, ramp, gate result, metering windows, the month-fit decision per arm (with the month it was taken for, and whether it is a preview), deleted imports. Only the submitter writes it |
| `commands.log` | operator commands, applied at the next cycle |
| `warm/<import>.json` | the pinger's warm-up record |
| `s4.log` | one line per attempt: time, arm, episode key, attempt label, `result` or `retry` |
| `gate/<time>.txt` | the gate command's output |
| `events.log` | submissions, rule triggers, applied commands |

**Resume** by running `e5 run` again from anywhere. The submitter reads the ledger and the results, adopts any job it submitted but didn't record, and submits only open entries. An entry is open when:

- it has no result;
- it has had fewer than 3 attempts, or 3 submissions;
- it has no live job;
- it has no harness-failed attempt still waiting 5 minutes for its `score.json`.

So a restart never resubmits an episode that has a result or is running.

**Operator commands**, all applied at the next cycle:

| Command | Effect |
|---|---|
| `e5 status` | the heartbeat, pauses, gate, windows and warm-up records |
| `e5 pause <arm> --reason "..."` | stop topping the arm up |
| `e5 resume <arm>` | resume; S-2, S-3 and S-8 then look only at attempts after now. For a month-fit arm, the §10.4 decision is taken again |
| `e5 clear-stop` | lift a hard stop. Only after Mahmood's decision |
| `e5 gate --rerun` | evaluate the S-1 gate again (after a dated v3 amendment) |
| `e5 window <arm> start` / `end [--usd X]` | open or close an external arm's metering window. `--usd` records spend outside AWS |

## 5. The meter

```bash
meter now        # before each arm, and hourly
```

The meter has three parts:

- tokens on the stack's tagged inference profiles × price, read in each profile's own region (us-east-2 for Claude Sonnet 5.5, from its `models.yaml` entry; us-east-1 for the rest);
- imported-model copy-minutes × $0.114;
- Batch attempt run time × 2 vCPU × $0.025, plus attempts still running.

It lags about 1 to 5 minutes. The month estimate is the metered spend plus spend recorded outside AWS. In October it is the AWS Budgets actual plus the meter since 24 h before the actual's last update. Per-arm spend is the same meter over the arm's windows, plus the arm's own job time.

Claude Sonnet 5.5 is priced at its us-east-2 regional list price, $2.20 / $11 per million input / output tokens. Its tokenizer yields about 30% more tokens per text than Sonnet 4.6's, and that is already in the counted tokens. AWS bills it as AWS Marketplace software usage (usage types `USE2-MP:…`), so it may not show under Bedrock in Cost Explorer, and the tag-filtered AWS budget may not count it. For that arm the metered S-9 stop is the guard. The day after its first calls (the smoke and canary), compare Cost Explorer's Marketplace charges for it with `meter now`.

Reading us-east-2 needs nothing extra from OperatorRole: its `cloudwatch:GetMetricData` and `cloudwatch:ListMetrics` grant is on `*` with no region condition, and those actions have no resource-level permissions. `e5 preflight` fails with "the meter is broken" if a read is refused in either region.

`meter --as-caller --dry-run validate --sweep e4-claude` checks the meter on E4 history: the episode traces' tokens and the jobs' run time against CloudWatch. Copy-minutes at $0.114 read about 30% above Cost Explorer for Oct 13–15, so the meter overstates import cost and stops early. After the canary days, compare one day with Cost Explorer and adjust `copy_minute_usd` in the config if needed.

## 6. Stop rules

| # | Trigger | The submitter | The operator |
|---|---|---|---|
| S-0 | pre-flight fails | doesn't start, or pauses the arm | fix, re-run `e5 preflight` |
| S-1 | the gate command exits non-zero | hard stop | page Mahmood; a dated v3 amendment is needed before any LLM episode |
| S-2 | > 3% harness failures in any rolling 100 attempts, or 3 consecutive `no_local_position` / `LinkLost` | pauses the arm | check the hosts and image; `e5 resume` |
| S-3 | > 2% invalid replies in the arm's first 100 episodes | pauses the arm | check for client or throttling errors; resume if the model is the cause |
| S-4 | throttles > 1% of calls over 15 min | cuts the arm's cap by 5 | none |
| S-5 | warm-up fails after 30 min | doesn't submit the arm | `e5 ping --restart` later |
| S-6 | copy up for 10 min, none of its episodes running | stops pings | `e5 unkill` if the arm still has work |
| S-7 | projected arm spend > its cap (after 100 settled, then every 250) | continues if the month still fits without the month-fit arms not yet started, and records which of them (in order) it pushes to December; otherwise pauses | page Mahmood on a pause |
| S-8 | a month-fit arm's canary > $0.060/episode, or its rolling 100-episode cost > 1.5 × canary | doesn't admit the arm, or pauses it | resume only if the §10.4 sum still fits |
| S-9 | November metered > $400; October estimate > $420; December metered > its `stop_usd` | hard stop on all submissions | page Mahmood |
| S-9 | no `stop_usd` for the month (December until Mahmood sets it) | submits nothing; no hard stop | none |

In October, an arm also doesn't start if the estimate plus its P90 would exceed $420.

**§10.4 month fit.** It covers the month-fit arms in order: GPT-5.6 Sol first, then Claude Sonnet 5.5. The committed spend is the metered spend this month, plus the other arms' remaining projected cost, plus $10 of retries. Each arm's projection is 1.15 × (its open episodes × canary $/episode + its probe).

- **GPT** runs if the committed spend plus its projection is at most $400.
- **Sonnet** runs if the committed spend plus GPT's projection (when GPT runs this month) plus its own is at most $400.
- **An arm that doesn't fit** goes to December, if December's `stop_usd` covers its projection on top of any arm deferred before it. Arms keep their order, so if GPT goes to December, Sonnet goes too. An arm that isn't admitted is left out, and the next arm is summed without it.
- **December's `stop_usd`** is set by Mahmood, up to about $375 (GPT and Sonnet at most about $190 each). The config lists December's four arms without a stop. Until a stop is set, the deferred arms wait, and nothing is submitted in December. If none is set by Nov 20, the deferred arms are dropped.

The sum is taken when GPT is about to start. At that point Sonnet also gets a preview decision, so a December request can go to Mahmood early (at the plan's medians, Sonnet is expected to go to December). Sonnet's own decision is taken again when GPT has settled, with GPT's spend measured by then. A decision is kept until the month or a budget in the config changes. `e5 resume <arm>` makes the submitter take it again. `e5 month-fit` prints every month-fit arm's sum at any time, and `e5 status` shows the recorded decisions.

## 7. The S4 rule

Each cycle classifies new attempts. An attempt is the episode's result unless it is a harness failure, or it is a harness failure whose ULog shows a violation (design §8). A harness-failed attempt is classified once its `score.json` is there, or 5 minutes after its `episode.json`. Only the verdict, `result` or `retry`, is written to `s4.log`. Nobody reads the violation flag. An episode with no result after 3 attempts is missing.

## 8. Settlement and the end of the wave

- An arm settles when every entry has a result or 3 attempts, and nothing is live. Its metering window closes then, and its import is deleted once every arm sharing it has settled.
- Probes and the labeller: run `e5 window <arm> start` before and `e5 window <arm> end` after. Add `--usd` for spend outside AWS.
- `e5 run` exits when every Batch arm has settled, been dropped, failed admission, or waits for next month. It names the waiting arms. Check `e5 status` and `meter now`, then confirm in the Bedrock console that no import is left.
- **December:** once Mahmood has set `stop_usd` for `"2026-12"` in the config, run `e5 preflight` and then `e5 run` again on December 1 or later. GPT goes first, then Sonnet. The December stop is enforced like November's.
- The one pinned analysis run, `lip stats --design v3`, comes only after every arm, including any deferred to December, has settled or been dropped (§2.5).
- No infra, AMI or stack change from the WP17 deploy until the wave ends.

## 9. Who to page

Page Mahmood for:

- a hard stop (S-1, S-9);
- an S-7 pause;
- a deferral or drop of GPT-5.6 Sol or Claude Sonnet 5.5, including a Sonnet preview deferral (December's `stop_usd` must be approved by Nov 20);
- any S-0 failure that needs an IAM or budget change.

The submitter keeps writing its heartbeat while stopped, so the pingers stand down. Nothing more is submitted until `e5 clear-stop`.
