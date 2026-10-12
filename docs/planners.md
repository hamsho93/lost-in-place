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

## The built-in LLM planner

`lost_in_place.llm` turns any model in the registry into a `Planner`:

```python
from lost_in_place.planning import load_planner

planner = load_planner("llm", model="claude-sonnet-5-5")
```

- **One request per decision.** The observation, including its history, is rendered into a single user message. At observation level O0 it shows what an as-deployed planner sees. O1 adds an estimator-health block. Each action is one tool, and the model must call exactly one.
- **Invalid replies are results, not crashes.** No tool call, two tool calls, an unknown tool or bad arguments raise `InvalidProposal`, so the harness can count them.
- **Every decision is recorded** in `planner.decisions`: model and resolved model id, prompt version and hash, the prompt itself, the raw reply, tokens, latency and the parsed action.
- The prompt is a draft (`PROMPT_VERSION = "v0-draft"`) until the spec RFC freezes it.

Try a model without the simulator. This makes one real call:

```bash
uv sync --extra aws
uv run lip models                                         # what's in the registry
uv run lip propose claude-sonnet-5-5 --health degraded    # one decision, printed as JSON
```

On AWS, assume the stack's operator role and export the stack's `BedrockProfiles` output as `LIP_BEDROCK_PROFILES`, so the call goes through the tagged profile and counts against the budget.

## Adding a model

**On Bedrock:** add an entry to [`src/lost_in_place/llm/models.yaml`](../src/lost_in_place/llm/models.yaml) with the provider set to `bedrock` and the model's system inference profile id. Then:

1. Enable model access for it in the Bedrock console.
2. Redeploy the infra. The stack reads the registry and creates a tagged profile and the permissions for every Bedrock model in it.
3. Check it with `lip propose <name>`.

```yaml
claude-opus-5-5:
  provider: bedrock
  model_id: us.anthropic.claude-opus-5-5
  temperature: 0
```

**On another provider** (e.g. OpenAI, or an OpenAI-compatible server for local models):

1. Write a client in `llm/clients.py` with `complete(system, user, tools) -> Completion`, converting the provider-neutral tool specs into the provider's format.
2. Register a factory for it in `CLIENTS`.
3. Add registry entries with that provider name.

The planner, prompt, parsing and traces are shared, so every model is scored on the same footing. Spend outside AWS isn't covered by the stack's budget, so cap it with the provider.

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
