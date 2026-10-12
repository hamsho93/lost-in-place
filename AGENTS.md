# Agent guide

Lost in Place is a PX4 + Gazebo benchmark. It checks whether an LLM drone planner, and its safety validator, notice when the autopilot's position estimate is wrong. Read [README.md](README.md) for the pitch, and [docs/fidelity.md](docs/fidelity.md) before quoting or changing any number.

## Layout

| Path | What it is |
|---|---|
| `src/lost_in_place/` | The `lip` CLI (`cli.py`), episode runner (`episode.py`, `sim.py`, `slots.py`), scenarios, metrics, worlds, planner interfaces (`planning.py`) |
| `src/lost_in_place/llm/` | LLM planner: model registry (`models.yaml`), provider clients, prompt and tools, `LLMPlanner` |
| `tests/` | Unit tests. They need neither PX4 nor Gazebo |
| `scenarios/fixtures/` | One YAML per scenario. New faults need a fixture here |
| `px4/` | Pinned PX4 commit (`PX4_COMMIT`) and the patches applied to it (`apply.sh`) |
| `docker/Dockerfile` | Headless x86-64 image: Gazebo Harmonic, patched PX4 SITL, the runner. Runs as user `lip` |
| `infra/` | AWS CDK app (`LostInPlaceSim`). See [infra/AGENTS.md](infra/AGENTS.md) before touching it |
| `.github/workflows/` | `ci` on every push; `image` builds the container (weekly and on demand) and pushes to ECR from `main` |

## Commands

```bash
make setup     # uv sync for the package and infra/
make check     # ruff check, ruff format --check, mypy (strict, src/), pytest, infra ruff + pytest
make synth     # cdk synth
```

Run `make check` before every commit; CI runs the same steps. Use `uv run ...`, not bare `python`.

## Conventions

- Python 3.11+, fully typed (mypy strict on `src/`), `ruff format`, line length 110.
- Commit messages: `type(scope): description`, e.g. `fix(runner): ...`, `docs: ...`, `infra: ...`. The body says why.
- Keep comments for constraints the code can't show. Don't narrate.
- Simulator behaviour that differs from a real drone goes in the known-issues table in `docs/fidelity.md`.
- Never report a result from a single seed. Report counts and intervals (`metrics.wilson_interval`).
- Don't commit `runs/`, ULogs (`*.ulg`) or anything from `infra/cdk.out/`.

## Things that are easy to get wrong

- **The AWS account ID never goes into git**, including commit messages and PR text. `infra/cdk.context.json` is gitignored for this reason. Workflows read account-specific values from repository variables.
- **Mission timing is sim time.** PX4 runs in lockstep with Gazebo at about 0.2 to 0.4 times real time. Use `wait_sim`, never wall-clock sleeps, for anything in a scenario.
- **Parallel episodes need separate slots.** Each slot (PX4 instance number) has its own MAVLink, MAVSDK and Gazebo partition ports. `lip run` claims a free one by default. AWS Batch uses host networking, so slots must stay unique per host, not just per container.
- **Every episode starts clean.** The PX4 work dir under `/tmp/lost-in-place` is wiped on start, because parameters persist there.
- **x86-64 only** until the flow-quality fix (`px4/patches/0001`) is upstream: the bug it fixes is undefined behaviour that can differ on ARM.
- **`lip doctor` writes nothing**, so it can't catch permission problems in the image. The `image` workflow also checks that `runs/` is writable.
- **PX4 changes go in `px4/patches/`** as numbered patch files that `git apply` accepts, applied in order by `px4/apply.sh`, each with a row in `px4/README.md`. Never rely on edits to a local PX4 checkout.
- **Planner models are pinned.** Each arm in `llm/models.yaml` names an exact model version; never point an existing arm at a different model, add a new arm instead. Any change to the prompt, the rendering or the tools bumps `PROMPT_VERSION` in `llm/prompt.py`.
- **Tests never call a real model.** Use a fake client (see `tests/test_llm.py`). `lip propose <model>` makes one real call and costs real money.
- **A container image build takes about 11 minutes** on GitHub (PX4 compiles from source). Test Dockerfile changes by dispatching the `image` workflow on your branch; it only pushes from `main`.
