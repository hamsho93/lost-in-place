---
name: triage-harness-failures
description: >-
  Diagnose episodes that end in harness failures (boot_stall, stuck_on_ground, sim_stall,
  inject_failed, no_local_position, arming_denied, timeout, error: ...) locally or on AWS Batch,
  using each attempt's uploaded files. Use when a sweep's harness-failure rate is high, when
  mavsdk_server dies, or when an episode crashes or hangs.
---

# Triage harness failures

A harness failure is an outcome the vehicle and planner didn't cause (`manifest.HARNESS_FAILURES`). The attempt rule retries them, so a high rate costs money and turns episodes into "missing". If one cell has more failures than the others, its results may be biased too.

## 1. Count before you read logs

```bash
uv run python .cursor/skills/review-sweep/scripts/breakdown.py mirror s3://$bucket/runs --manifest <m> --out /tmp/m
uv run python .cursor/skills/review-sweep/scripts/breakdown.py breakdown /tmp/m --manifest <m> | head -20
```

The first section gives outcomes per image (git SHA). Check whether failures cluster by image, by cell, by instance type or on a fresh host. Batch's `statusReason` names the host type for reclaims.

## 2. Read one failing attempt

Each attempt uploads to `runs/<key>/<job id>-<index>-attempt<n>/`:

| File | Look for |
|---|---|
| `episode.json` | `outcome`, the last few `events` (each has `wall_s` and `sim_s`, so you can compute the real-time factor at the failure), `mavsdk_server_exit`, `retried`, `real_time_factor`, `provenance` |
| `mavsdk_server.txt` | `heartbeats timed out`, gRPC errors, the last line before the server died |
| `px4_console.txt` | Preflight failures, EKF2 messages, failsafe, the injection ack |
| `flight.ulg` | Truth vs estimate (`lip analyze`, or `pyulog`) |

## 3. Known signatures

| Signature | Cause | Fix or handling |
|---|---|---|
| `error: AioRpcError ... failed to connect` or `inject_failed`, `mavsdk_server_exit: -6`, and "heartbeats timed out" every few seconds in `mavsdk_server.txt` | PX4's heartbeat runs on sim time. Below about 0.33× real time, a 1 Hz heartbeat misses MAVSDK's 3 s wall-clock timeout, and `mavsdk_server` aborts on the next command | The airframe's `.post` script (`docker/4021_gz_x500_flow.post`) sets a 5 Hz heartbeat at boot. Check that the image contains it. Never set stream rates from the runner over MAVSDK: that caused boot stalls in 13 of 17 attempts |
| A good run still logs "heartbeats timed out" | The `.post` script isn't installed, or the real-time factor is below about 0.07× | Check the Dockerfile `COPY`, and `real_time_factor` |
| gRPC "Stream removed" 12 to 50 s in, on a fresh host | Cold-start contention, mostly on AMD `c7a` | AMD is excluded from the compute environment. Batch retries the attempt |
| `no_local_position` after "Preflight Fail: no valid barometer data", with a slow first 12 s of sim | Cold host | Retry. The barometer message by itself is harmless: it appeared in 8 of 12 good runs |
| `stuck_on_ground` | Gazebo ignored the motors | The runner reboots the sim, up to three boots. Cause unknown |
| `boot_stall`, recovered by a reboot | Slow boot | Normal at a low rate. If it is frequent, look for anything the runner sends before the first heartbeat gap |
| No files at all; Batch status "Host EC2 ... terminated" | Spot reclaim | Batch retries the attempt. Nothing to fix |
| `timeout` or `sim_stall` | The sim stopped advancing, or ran too slowly to finish | Compare `real_time_factor` and `wall_s` with the sweep median. Check how many episodes were packed on the host |
| Local Docker run: permission denied writing `runs/` | The container runs as non-root user `lip` | `chmod 777` the mounted directories |

## 4. Reproducing

- **Timing bugs need Batch.** Local runs at `--cpus=2` give about 0.37× real time, and `--cpus=1` stalls boot. Canary on Batch with a small array (see the `run-sweep` skill).
- **Logic bugs reproduce locally.** Take the scenario, arm and seed from the episode key (`<scenario>/<planner>-<validator>-<obs>/r<rep>-s<seed>`): `uv run lip run scenarios/<dir>/<scenario>.yaml --seed <seed> --planner <planner> --validator <validator> --obs <obs> --analyze`.

## 5. After the fix

- Add a row to the known-issues table in `docs/fidelity.md`, with numbers.
- Quarantine attempts from a broken image (see `run-sweep`), then resubmit.
- If the cause is MAVLink timing, check that the paused-world path still sends heartbeats: an LLM planner call pauses Gazebo for seconds.
