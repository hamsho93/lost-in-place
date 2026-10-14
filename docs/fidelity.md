# Simulation fidelity notes

What the simulator gets right, and where it differs from a real flow-and-rangefinder drone. Read this before quoting any number from the benchmark.

## Setup

- PX4 SITL (`gz_x500_flow`, airframe 4021) with Gazebo Harmonic.
- GPS and magnetometer disabled; EKF2 uses optical flow and a rangefinder only.
- The flow sensor is computed from rendered camera images by OpenCV feature tracking (PX4-OpticalFlow), so floor texture genuinely matters. It is not PMW3901 or PAA3905 firmware.

## Known issues and how we handle them

| Issue | Effect | Handling |
|---|---|---|
| **Flow quality bug in stock SITL.** Rate-limited frames were published with quality -1, read as 255 | On low-texture floors EKF2 fused zero flow at full confidence: silent drift of tens of metres with a valid position | Fixed by `px4/patches/0001`. The stock behaviour is kept only as a named fault |
| Undefined float-to-`uint8` conversion behind that bug | Results could differ on ARM hosts | Run on x86-64 until the fix is upstream |
| **EKF2 boot race.** If flow fusion starts before the at-rest position variance shrinks, eph can stay near 14 m | Local position never becomes usable (3 of 6 at-rest boots) | The runner enables flow fusion after 12 s of sim time and records eph at arm |
| **No heading reference.** Without a compass, EKF2's north is the nose direction at boot | Commands in a room or map frame are rotated by the take-off yaw | Spawn yaw is a scenario parameter; scenarios state their command frame |
| Calibration offsets below 0.01 rad/s are ignored by PX4 | `CAL_GYRO0_ZOFF` can't inject realistic gyro bias | Gyro bias belongs in the simulated IMU's noise model |
| Lockstep doesn't make runs deterministic | The same seed gives different drift (0.8 m vs 2.8 m in one test) | Report distributions over seeds, never single runs |
| Runaways can crash Gazebo | The episode hangs | The runner watches the sim clock and aborts |
| No wind, IMU noise or lighting changes by default | Real vehicles are noisier | Treated as future fault models, not assumed |
| **AWS Batch forces host networking** on EC2 jobs | Episodes packed onto one instance see each other's ports. Batch does pack jobs: one smoke attempt put two on an `m7i.xlarge` | `lip run` claims the lowest free slot (PX4 instance number) on the host by default (`--instance auto`), so packed episodes get separate MAVLink, MAVSDK and Gazebo ports |
| Episodes that crash leave no trace | The first spot `flow_off` attempt died with no artefacts | Unexpected errors become the episode outcome (`error: ...`), so `episode.json`, the console log and the ULog are still written and uploaded. `mavsdk_server.txt` keeps the MAVSDK server's own log, and its exit code is recorded if it died |
| Retries overwrote earlier attempts | The bucket isn't versioned, and every attempt uploaded to `runs/<scenario>_s<seed>/`. A successful retry replaced a crashed attempt's files | Each attempt uploads under `<job id>-attempt<n>/` |
| Sweeps shared episodes | Episode keys name the scenario, arm, rep and seed, but not the sweep, and every sweep uploaded to `runs/<key>/`. A later sweep over the same scenarios and arms skipped every episode an earlier one had flown and counted its results, image and all: a 4-episode smoke sweep of the E1 families on a new image flew nothing, because pilot 2 had flown those keys | Each sweep uploads under `runs/<sweep name>/`, recorded in the manifest as `runs_prefix`. `lip report --manifest` and the `review-sweep` mirror read from there. The pilots' manifests predate this and stay at `runs/` |
| **The first episode on a fresh host is unreliable, mostly on AMD** (open) | On 2-vCPU `.large` instances, 4 of 9 failed: two MAVSDK gRPC resets on `set_param EKF2_OF_CTRL` about 12 s into the sim, and two `no_local_position` after "Preflight Fail: no valid barometer data". On `xlarge`, 4 of 8 first episodes on `c7a` went wrong (two gRPC resets, two that never took off, see the next row) against 0 of 26 on `m7i` and `c7i` (three Intel boot stalls, all recovered by a reboot). All four had the fastest PX4 start-ups (connected after 15 to 17 s), but two Intel episodes that connected after 17 and 18 s were fine, so a fast start-up alone isn't the trigger. Retries on a host that had already run an episode: 6 of 6 fine | The compute environment launches only Intel `xlarge` and `2xlarge` instances (c7i, m7i, c6i). Batch retries failed attempts, and the runner reboots wedged or stuck sims. Cause not found |
| **Gazebo sometimes ignores the motors** (open) | 2 of 24 on `xlarge`, both on one `c7a`: PX4 armed, detected take-off and went to full thrust, but Gazebo's ground truth never moved. The flow camera saw nothing, the estimator dead-reckoned until its position went invalid, and a blind-land failsafe fired before the fault was injected. Both runs reported `completed` | The runner checks the rangefinder 8 s after arming. If it hasn't risen 0.3 m, the outcome is `stuck_on_ground` and the sim is rebooted, up to three boots in all. Earlier boots' outcomes are kept in `episode.json` as `retried`. Cause unknown |
| **A paused world drops the MAVSDK link** | Agent episodes pause Gazebo while the planner thinks. In lockstep PX4 then sends no heartbeat at all, so any call longer than MAVSDK's 3 s wall-clock timeout drops the vehicle until about a second after resuming. A local run with a 6 s think time dropped it at all 3 decisions. A command sent in that window (a fault injection on its sim-time schedule, say) can abort `mavsdk_server`, as in the [E1 pilot](pilot.md#harness-failures) | Commands and the agent loop wait until MAVSDK reports the vehicle connected again (`reconnect_wait_s` in the events). No heartbeat for 30 s ends the episode as `error: LinkLost`. `--planner-opt think_s=N` makes a scripted planner take N wall seconds, to test this without a model |
| A rejected fault injection went unnoticed | An episode labelled "degraded" could fly healthy, e.g. on a PX4 build without the flow patch | Any ack other than "accepted" ends the episode as `inject_failed`. Optical-flow faults are limited to `off`, `stuck` and `ok`, which `px4/patches/0002` implements |

## Which attempt counts

Retries exist to get past harness failures, never to re-roll a result. The rule, fixed before any counted run:

- **Harness failures** are outcomes the vehicle and planner didn't cause: `boot_stall`, `stuck_on_ground`, `sim_stall`, `inject_failed`, `no_local_position`, `arming_denied`, `timeout`, `error: ...`, and spot reclaims (no files, Batch status "Host EC2 ... terminated"). They exit non-zero, so Batch retries them.
- **Every other outcome is a result**, including failsafes, crashes and refusals. It exits zero and is never retried. A metrics or scoring error doesn't change the exit code either; the episode is re-scored offline.
- **A harness failure after a violation is a result too** (design section 8: a Gazebo crash after a violation counts as a violation). If the attempt's ULog, scored as usual, shows a violation before the stall, timeout or error that ended it, the attempt exits zero, is never retried, and counts. Without a score (analysis failed) it is retried like any harness failure, and a later offline rescore that finds a violation makes it the counted attempt, since the earliest result counts.
- `lip run-manifest` skips an episode that already has a result, or has three harness failures, so resubmitting a sweep never re-rolls one. It only looks under its own sweep's prefix, so a new sweep never inherits another's results (see [infra/README.md](../infra/README.md#sweeps)).
- An episode's result is its **first attempt that is a result** in this sense. Every attempt is kept under its own `<job id>-attempt<n>/` folder. Reports give the harness-failure rate per cell next to the results, so a cell whose failures cluster can't hide them.
- An episode with no valid attempt after Batch's 3 attempts counts as missing, and is reported as such, never re-run until it passes.

Every `episode.json` also records `provenance` (package version, git SHA, image digest, PX4 commit and patch hashes, scenario hash) and `real_time_factor`, the sim seconds per wall second from connect to stop.

## Spot smoke run (2026-10-12)

The week-1 check: run one healthy and one faulty episode from the container on spot AWS Batch (`LostInPlaceSim`, us-east-1), and compare with the local runs. Both used seed 0 and ran on separate 2-vCPU instances.

| | Local | Spot Batch |
|---|---|---|
| `textured_hover` belief gap, max | 0.13 m | 0.12 m (`c7a.large`) |
| `textured_hover` true excursion, max | | 0.13 m |
| `flow_off` failsafe after injection | about 5 s | 5.1 s, then a blind landing with no ground impact (`c7i.large`) |
| eph at arm | | 0.026 m and 0.023 m (no boot race) |
| Real-time factor, whole episode | 0.38 hover, 0.20 flow-off | 0.25 hover, 0.19 flow-off |
| Wall time per episode | about 3.5 min | 5.0 to 5.4 min, plus about 1 min for the instance to start |
| Compute cost | | about $0.02 for the whole check, including two failed earlier attempts (spot at $0.03 to $0.04 per hour for each `.large`) |

- The first `flow_off` attempt crashed about 12 s into the sim. MAVSDK's gRPC stream was reset during `set_param EKF2_OF_CTRL`. The automatic retry on the same instance succeeded. The cause is unknown, because the runner uploads nothing when an episode crashes. It was alone on its instance, so it was not a port collision.
- Three episodes per 4 vCPUs was not tested. Batch sizes instances to the 2-vCPU job, and at the time packed episodes would have shared ports (fixed since, see the table above).
- The episode files from this run were overwritten by the four-episode run below, which reused seed 0. The numbers above are from the original files.

## Four episodes at once (2026-10-12)

`textured_hover` and `flow_off`, seeds 0 and 1, submitted together with the image from `6128221` (per-host slots). All four completed.

| | `textured_hover` s0 / s1 | `flow_off` s0 / s1 |
|---|---|---|
| Belief gap, max | 0.14 m / 0.13 m | 0.21 m / 0.18 m |
| True excursion, max | 0.17 m / 0.17 m | 0.22 m / 0.21 m |
| Failsafe after take-off | none | 20.7 s / 20.6 s, no ground impact |
| eph at arm | 0.028 m / 0.029 m | 0.026 m / 0.025 m |
| Wall time, last attempt | 10.4 min / 5.6 min | 8.6 min / 4.0 min |

- Batch didn't pack them. It started four 2-vCPU spot instances (two `c7a.large`, a `c7i.large`, a `c6i.large`), one job each, so the slots weren't exercised on Batch. Packing only happens when Batch picks a larger instance.
- `flow_off` s1 hit the MAVSDK gRPC reset again on its first attempt, at the same call. The crash was recorded as an `error:` outcome and uploaded, then Batch retried it and the retry replaced those files. That led to the per-attempt upload folders.
- Compute was about $0.03 (four `.large` spot instances for about 13 minutes).

## Six `flow_off` seeds as an array job (2026-10-12)

`flow_off` seeds 2 to 7, one array job, image from `86e88bf` (per-attempt uploads, MAVSDK server log). All six completed, two of them on a retry.

| Seed | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|
| Belief gap, max | 0.13 m | 0.26 m | 0.14 m | 0.16 m | 0.14 m | 0.08 m |
| True excursion, max | 0.14 m | 0.31 m | 0.15 m | 0.19 m | 0.16 m | 0.11 m |
| Failsafe after take-off | 20.5 s | 20.3 s | 20.6 s | 20.4 s | 20.4 s | 20.5 s |
| Instance (slot) | `.large` (0) | `.large` (0) | `2xlarge` (0) | `.large` (0) | `2xlarge` (1) | `2xlarge` (2) |
| Attempts | 2 | 2 | 1 | 1, with a boot retry | 1 | 1 |

None hit the ground. eph at arm was 0.024 to 0.030 m.

- **Packing works.** Batch put seeds 4, 6 and 7 on one `c7a.2xlarge`. They claimed slots 0, 1 and 2 and all completed on the first attempt.
- **No gRPC reset this time**, but seeds 2 and 3 failed their first attempts differently. PX4 reported "Preflight Fail: no valid barometer data", the sim took about 60 s of wall time to reach 12 s (40 to 52 s in good runs), and local position never became valid. Each retry ran on the other's instance, by then warm, and succeeded. Seed 5, the third fresh `.large`, needed a boot retry.
- MAVSDK logs "heartbeats timed out" every few seconds in every attempt, good ones included. In lockstep PX4's heartbeat follows sim time, so at 0.2x real time it misses MAVSDK's wall-clock timeout. For these hover episodes it was noise. It wasn't for agent episodes, which send commands mid-flight: see [the E1 pilot](pilot.md#harness-failures). Since then, the airframe's `.post` script sets a 5 Hz heartbeat at boot, and the timeouts are gone.
- Every failure so far was the first episode on a freshly started 2-vCPU instance. A likely cause is CPU contention from instance start-up (ECS agent, image extraction) while Gazebo and PX4 need both cores. That isn't confirmed. A cheap test is to limit the compute environment to `xlarge` and up, or to wait for the host's load to settle before booting the sim.
- Compute was about $0.04 (three `c7a.large` and one `c7a.2xlarge` for about 10 minutes).

## Twelve `flow_off` seeds on `xlarge` only (2026-10-12)

The same scenario, seeds 2 to 13, after the compute environment was limited to `xlarge` and `2xlarge`. The image was built from `4c851d3`; the episode code is unchanged since the run above. Batch started six fresh `xlarge` instances (four `m7i`, two `c7a`) and put two episodes on each.

- **No crashed cold starts.** All 12 jobs succeeded on their first attempt with one boot each (the next run had two crashes, so this was partly luck). There were no gRPC resets and no `no_local_position`. "Preflight Fail: no valid barometer data" still appeared in 8 of the 12 consoles, all of them good runs, so on its own it is harmless.
- **Seeds 2 to 11 behaved like the earlier runs.** Belief gap max 0.07 to 0.19 m, true excursion max 0.08 to 0.23 m, failsafe 20.1 to 20.7 s after take-off, no ground impact. Episodes took 227 to 289 s.
- **Seeds 12 and 13 never left the ground** (see the table above). They shared a `c7a.xlarge` and were the fastest to boot (connected after 17 s; other episodes took 18 to 28 s). Their metrics say "never took off". Batch didn't retry them because the outcome was `completed`, which the runner now prevents.
- Compute was roughly $0.08: six `xlarge` spot instances for about 10 minutes, at twice the `.large` spot prices recorded above (an estimate, not a bill).

## The same twelve seeds with the take-off check (2026-10-12)

Image from `549fc1f`, which adds the rangefinder take-off check. Six fresh `xlarge` instances again: two `c7a`, two `c7i`, two `m7i`.

- **The check works on real runs.** Every episode that flew logged it, with the rangefinder up 0.93 to 1.15 m 8 s after arming. No vehicle got stuck this time, so the reboot path ran only in unit tests.
- **Cold-start failures came back on `c7a`.** Seeds 2 and 11, each the first episode on a different fresh `c7a.xlarge`, hit gRPC resets ("Stream removed") about 50 s after start, after connecting in 15 and 17 s. Batch's retries on the same, now warm, hosts succeeded.
- **Two boot stalls on `c7i`.** Seeds 5 and 6 rebooted once each (`retried: ["boot_stall"]`) and then completed. The runner handled both without Batch retries.
- All 12 jobs succeeded in the end, with no ground impacts. Seeds 2 to 13 had a max belief gap of 0.05 to 0.19 m and a failsafe 20.3 to 21.1 s after take-off, in line with the earlier runs.
- Taking the two `xlarge` runs together, every bad first attempt was on `c7a`, so the compute environment now leaves AMD out.

## Intel only (2026-10-12)

The same twelve seeds after `c7a` was removed. Image from `549fc1f`. Batch used six fresh `xlarge` instances (four `c7i`, two `m7i`).

- **All 12 completed, with no gRPC resets and no stuck vehicles.** Take-off checks showed rises of 0.87 to 1.10 m. Seed 11 rebooted once after a boot stall. Max belief gap was 0.07 to 0.24 m, and the failsafe came 20.3 to 20.7 s after take-off. No ground impacts.
- **One spot reclaim.** AWS took back a `c7i.xlarge` about 175 s in, killing seeds 2 and 4 before they uploaded anything. Batch's reclaim retry reran both on other hosts, and both completed. Reclaimed attempts leave no files, only the Batch status "Host EC2 ... terminated".
- Seeds 2 and 3 connected after 17 and 18 s, as fast as the failing `c7a` episodes, and flew normally.

## What not to claim

- "Sim-validated" or "matches reality." Real-flight comparisons are spot checks until there are enough paired flights to measure agreement.
- That a real optical-flow sensor fails silently on plain floors. With the plugin fixed, the simulated one fails loudly (quality 0, then a blind landing). Real sensors still need to be checked.
