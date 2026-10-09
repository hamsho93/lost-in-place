# Using your own planner

The benchmark only needs a planner to **propose** one action per step. It decides separately which validator checks that proposal, and it executes the action in simulation. That separation is what lets the benchmark compare "validator off" and "validator on" without touching the code that flies your real drone.

## The interface

From `lost_in_place.planning`:

```python
class Planner(Protocol):
    async def propose(self, observation: Observation) -> Action: ...
```

- `Observation` carries the goal, the estimated pose, battery and recent history.
- At the health-visible level it also has an `EstimatorHealth` block: whether the position is valid, eph, flow quality, whether an innovation check is failing, and whether there is a heading reference.
- `Action.name` is one of `take_off`, `move`, `rotate`, `hold`, `land` or `refuse`.

## Plugging in a planner that lives in another repo

Your project depends on this one, never the reverse. Register a factory under the `lost_in_place.planners` entry point in *your* `pyproject.toml`:

```toml
[project.optional-dependencies]
bench = ["lost-in-place"]

[project.entry-points."lost_in_place.planners"]
my_planner = "my_package.benchmark_adapter:make_planner"
```

```python
# my_package/benchmark_adapter.py
from lost_in_place.planning import Action, Observation

class MyPlanner:
    async def propose(self, observation: Observation) -> Action:
        tool, args = my_existing_step(render_prompt(observation))  # your prompt + model call
        return Action(name=tool, args=args)

def make_planner(**options) -> MyPlanner:
    return MyPlanner(**options)
```

With both packages installed in one environment, `lost_in_place.planning.load_planner("my_planner")` finds it. This repo contains no code from your project.

## What to expose in an existing planner

If your planner runs a loop like `state → prompt → model → validate → execute`, factor out one function that stops after the model call. For example, `propose_action(state, history) -> (tool_name, tool_input)`.

- Your loop keeps calling it, then validates and executes exactly as before, so flight behaviour doesn't change.
- The benchmark calls only `propose_action` and applies its own validator arms.
- To show estimator health, add it as a new, opt-in prompt format rather than changing the existing one. Models fine-tuned on the old format keep working, and the change is measurable.

## Reproducibility

Results for a planner that isn't public can't be re-run by others. Publish its decision traces (prompts, responses, actions) with the results, and label that arm as a single-planner case study. Arms built from public models and the scripted planners are fully reproducible.
