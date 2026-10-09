# Contributing

Issues and pull requests are welcome, especially on scenarios, fault models and metric definitions.

## Development

```bash
make setup        # uv sync for the package and infra/
make check        # ruff, mypy (strict), pytest, infra tests
```

The unit tests don't need PX4 or Gazebo. To fly episodes locally, build PX4 with the patches in `px4/` (see [px4/README.md](px4/README.md)) and set `LIP_PX4_DIR`.

## Conventions

- Python 3.11+, typed, formatted with `ruff format`.
- Commit messages: `type(scope): description` (for example `feat(metrics): add belief gap at decision time`).
- New faults need an entry in [docs/fidelity.md](docs/fidelity.md) if they model something the simulator gets wrong, plus a fixture scenario.
- Never report results from a single seed; report counts and intervals.
