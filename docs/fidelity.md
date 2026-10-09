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

## What not to claim

- "Sim-validated" or "matches reality." Real-flight comparisons are spot checks until there are enough paired flights to measure agreement.
- That a real optical-flow sensor fails silently on plain floors. With the plugin fixed, the simulated one fails loudly (quality 0, then a blind landing). Real sensors still need to be checked.
