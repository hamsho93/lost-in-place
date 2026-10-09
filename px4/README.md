# PX4 patches

The benchmark runs upstream PX4 at the commit in [`PX4_COMMIT`](PX4_COMMIT), plus these patches.

| Patch | What it does | Upstream |
|---|---|---|
| `0001-gz-optical-flow-skip-rate-limited-frames.patch` | The Gazebo flow sensor published frames that PX4-OpticalFlow marks as "not yet" with quality -1, which became 255 in `sensor_optical_flow`. On low-texture floors EKF2 then fused zero flow at full confidence. With the fix, PX4 sees quality 0 and stops trusting flow, as with a real sensor. | Submitted as a PR to PX4-Autopilot |
| `0002-optical-flow-failure-injection-sitl.patch` | Adds `optical_flow` to the failure-injection catalogue and applies `off`, `stuck` and `wrong` (quality forced to 0) in the Gazebo bridge. SITL only. | Not yet; the upstream version belongs in `sensors/vehicle_optical_flow` so it also covers real flow drivers |

Apply them to a PX4 checkout:

```bash
px4/apply.sh ~/PX4-Autopilot      # checks out PX4_COMMIT and applies the patches in order
cd ~/PX4-Autopilot && make px4_sitl
```

The stock behaviour from before patch 0001 is still useful as a deliberate "silent false-zero" fault. To reproduce it, build without that patch.
