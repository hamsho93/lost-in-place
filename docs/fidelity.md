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
| **The first episode on a fresh 2-vCPU instance is unreliable** (open) | 4 of 9 failed: two MAVSDK gRPC resets on `set_param EKF2_OF_CTRL` about 12 s into the sim, and two `no_local_position` after "Preflight Fail: no valid barometer data". A fifth needed a boot retry. Retries on an instance that had already run an episode: 4 of 4 fine. Three episodes packed on a fresh `c7a.2xlarge`: 3 of 3 fine | Batch's retry recovers it, at the cost of an extra attempt. Cause not confirmed; see the run below |

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
- MAVSDK logs "heartbeats timed out" every few seconds in every attempt, good ones included. In lockstep PX4's heartbeat follows sim time, so at 0.2x real time it misses MAVSDK's wall-clock timeout. This is noise, not the failure.
- Every failure so far was the first episode on a freshly started 2-vCPU instance. A likely cause is CPU contention from instance start-up (ECS agent, image extraction) while Gazebo and PX4 need both cores. That isn't confirmed. A cheap test is to limit the compute environment to `xlarge` and up, or to wait for the host's load to settle before booting the sim.
- Compute was about $0.04 (three `c7a.large` and one `c7a.2xlarge` for about 10 minutes).

## What not to claim

- "Sim-validated" or "matches reality." Real-flight comparisons are spot checks until there are enough paired flights to measure agreement.
- That a real optical-flow sensor fails silently on plain floors. With the plugin fixed, the simulated one fails loudly (quality 0, then a blind landing). Real sensors still need to be checked.
