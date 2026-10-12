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
| **AWS Batch forces host networking** on EC2 jobs, and every episode runs as PX4 instance 0 | Two episodes placed on one instance would share MAVLink, MAVSDK and Gazebo ports. Batch does pack jobs: one smoke attempt put both on an `m7i.xlarge` | Not handled yet. Until the runner picks a free instance number, don't rely on more than one episode per instance |

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
- Three episodes per 4 vCPUs was not tested. Batch sizes instances to the 2-vCPU job, and packing more than one episode per instance needs the instance-number fix above first.

## What not to claim

- "Sim-validated" or "matches reality." Real-flight comparisons are spot checks until there are enough paired flights to measure agreement.
- That a real optical-flow sensor fails silently on plain floors. With the plugin fixed, the simulated one fails loudly (quality 0, then a blind landing). Real sensors still need to be checked.
