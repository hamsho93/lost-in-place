# Lost in Place

**When a drone's autopilot thinks it is holding still but is actually sliding away, does the LLM planner flying it notice, and does its safety checker?**

Lost in Place is an open benchmark for that question. It runs PX4 and Gazebo in simulation, where the true position is known exactly. It then degrades the vehicle's own position estimate (featureless floors, frozen or missing optical flow, no compass) while an LLM planner and a validator command the drone. We score whether the stack holds, lands or refuses instead of flying on.

> Status: **pre-alpha**. The harness and fault injection work; the scenario set and the pre-registered spec come next (published as an RFC before any results). There are no benchmark results yet.

## Why

Existing drone-LLM safety benchmarks test harmful *instructions* ("fly into the crowd"). Validators check commands against geometry in the *estimated* frame. Neither covers a benign instruction that becomes unsafe because the estimate is wrong. In our checks, a flow-and-rangefinder quadcopter (a DEXI-like setup) drifted tens of metres while PX4 reported a valid position.

## Quickstart

Needs Ubuntu 24.04 (x86-64), [Gazebo Harmonic](https://gazebosim.org/docs/harmonic/install_ubuntu) and a PX4 SITL build with the patches in [`px4/`](px4/).

```bash
uv sync
export LIP_PX4_DIR=~/PX4-Autopilot        # patched PX4, built with: make px4_sitl
uv run lip doctor                         # checks paths and Gazebo
uv run lip worlds                         # textured / low-texture / flat floors
uv run lip run scenarios/fixtures/textured_hover.yaml --seed 1 --analyze
```

Or use the container: `docker build -f docker/Dockerfile -t lost-in-place .`, then `docker run --rm lost-in-place run scenarios/fixtures/lowtex_hover.yaml --analyze`.

## Repository

| Path | What it is |
|---|---|
| `src/lost_in_place/` | Episode runner, scenario schema, metrics, world generator, planner and validator interfaces |
| `scenarios/` | Scenario files (YAML) |
| `px4/` | Pinned PX4 commit and the patches we apply to it |
| `docker/` | Headless image with PX4, Gazebo and the runner |
| `infra/` | AWS CDK app: spot Batch queue, results bucket, budget guard |
| `docs/` | [Simulation fidelity notes](docs/fidelity.md), [using it with your own planner](docs/planners.md) |

## Credits and related work

This project builds directly on:

- [PX4 Autopilot](https://github.com/PX4/PX4-Autopilot), its SITL, EKF2 and failure-injection framework.
- [Gazebo](https://gazebosim.org), [MAVSDK](https://github.com/mavlink/MAVSDK-Python), [pyulog](https://github.com/PX4/pyulog), and [PX4-OpticalFlow](https://github.com/PX4/PX4-OpticalFlow) (OpenCV KLT flow used by the simulated sensor).

It is informed by, and should be read alongside:

- [LLM Physical Safety Benchmark](https://arxiv.org/abs/2411.02317) (Tang, Chen & Ho, 2024): the direct prior work on drone-LLM safety. [α³-Bench](https://arxiv.org/abs/2601.03281) and [UAVBench](https://arxiv.org/html/2511.11252) are related.
- Validators and runtime assurance for LLM robots: [RoboGuard](https://arxiv.org/abs/2503.07885), [Safety Chip](https://arxiv.org/abs/2309.09919), [SELP](https://arxiv.org/abs/2409.19471), [PEACE](https://arxiv.org/abs/2606.00104), [Black-Box Simplex](https://arxiv.org/abs/2102.12981).
- Fault datasets and evaluation method: [RflyMAD](https://arxiv.org/abs/2311.11340), [ALFA](https://github.com/castacks/alfa-dataset), [SIMPLER](https://simpler-env.github.io/), and Robocurve's RoboHarm and Inspect evaluation tooling.
- Procedural worlds: [worldgen](https://github.com/griffinlabs-ai/worldgen), [ProcTHOR](https://procthor.allenai.org/), [Infinigen](https://github.com/princeton-vl/infinigen).

## License

MIT, see [LICENSE](LICENSE). Patches under `px4/patches/` modify PX4 and are BSD-3-Clause like PX4 itself.
