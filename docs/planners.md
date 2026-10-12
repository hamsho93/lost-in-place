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

planner = load_planner("llm", model="claude-sonnet-4-6")
```

- **One request per decision.** The observation, including its history, is rendered into a single user message. At observation level O0 it shows what an as-deployed planner sees. O1 adds an estimator-health block. Each action is one tool, and the model must call exactly one.
- **Invalid replies are results, not crashes.** No tool call, two tool calls, an unknown tool or bad arguments raise `InvalidProposal`, so the harness can count them.
- **Every decision is recorded** in `planner.decisions`: model and resolved model id, prompt version and hash, the prompt itself, the raw reply, tokens, latency and the parsed action.
- The prompt is a draft (`PROMPT_VERSION = "v0-draft"`) until the spec RFC freezes it.

Try a model without the simulator. This makes one real call:

```bash
uv sync --extra aws
uv run lip models                                         # what's in the registry
uv run lip propose claude-sonnet-4-6 --health degraded    # one decision, printed as JSON
```

On AWS, assume the stack's operator role and export the stack's `BedrockProfiles` output as `LIP_BEDROCK_PROFILES`, so the call goes through the tagged profile and counts against the budget.

## Flying a planner: agent episodes

`lip run` on a scenario with a `mission` (see `docs/scenarios.md`) runs a closed loop (`agent.py`):

```bash
uv run lip run scenarios/pilot/cross_silent.yaml --planner scripted-naive --obs O1 --analyze
uv run lip run scenarios/pilot/cross_silent.yaml --planner llm --planner-opt model=claude-sonnet-4-6
```

1. The harness arms and climbs to the scenario altitude, then checks the vehicle really left the floor.
2. **Observe.** Pose, battery and the last 8 actions (O0). With `--obs O1`, also the estimator's own view: `xy_valid` and eph from `ESTIMATOR_STATUS`, the largest velocity or position innovation test ratio, and the 2 s mean flow quality from `OPTICAL_FLOW_RAD`. They're read over the MAVSDK link with its MavlinkDirect plugin, so no second MAVLink port is needed.
3. **Pause and charge.** The Gazebo world is paused (`/world/<name>/control` in the slot's partition) while the planner and validator run, so a slow host or a slow model costs no simulation time. After resuming, the vehicle keeps flying its previous setpoint for `--latency` simulation seconds (2 s by default).
4. **Execute.** `move` and `rotate` run until the vehicle arrives or for one 1.5 s epoch, whichever comes first, then the planner is asked again. `hold` runs for its duration, up to 15 s. A rejected action is executed as a hold. `land` and `refuse` end the loop, and the harness lands.
5. The loop also ends when the planner-call budget or the scenario's `end` time runs out, or when PX4 takes the vehicle out of offboard control (a failsafe).

Faults are scheduled from arming or from the first executed move. Planner-flown missions cap horizontal speed at 0.7 m/s (`MPC_XY_VEL_MAX`). Above `EKF2_RNG_A_VMAX` (1 m/s), conditional range aiding stops, height falls back to the baro, and a healthy vehicle flew into the floor at a 1 m/s cap.

Every decision goes into `episode.json` under `decisions`: the simulation timestamp (the PX4 clock the ULog uses), what the planner was shown, the action, the verdict, and the planner's wall time. LLM calls also go under `planner_decisions`.

Two scripted arms ship as planners:

- `scripted-naive` flies the mission's waypoints, holds and lands, whatever the estimator says.
- `scripted-cautious` does the same, but lands when the health it is shown looks bad (position invalid, an innovation check failing, eph over 1 m or flow quality under 50). At O0 it is shown nothing, so it behaves like the naive arm.

## Adding a model

**On Bedrock:** add an entry to [`src/lost_in_place/llm/models.yaml`](../src/lost_in_place/llm/models.yaml) with the provider set to `bedrock` and the model's system inference profile id. Then:

1. Check the account can use it: `aws bedrock get-foundation-model-availability --model-id anthropic.<model>` must show `agreementAvailability` `AVAILABLE`. `NOT_AVAILABLE` means calls fail with "not available for this account" and only AWS Sales can change it; on this account that applies to every Claude 5.x model and to Opus 4.5 to 4.8 (checked 2026-10-12). Then enable access in the Bedrock console if it asks.
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
