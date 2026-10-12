# Scenarios

A scenario is a YAML file validated by `src/lost_in_place/scenario.py`. There are two kinds.

- **Scripted** (`scenarios/fixtures/`): `goto`, `inject`, `param` and `end` steps fly a fixed path. They're for the fidelity and manipulation checks.
- **Benchmark items** (`scenarios/pilot/`, spec v0.1): a `mission` gives the planner an instruction, and `hazard_truth` says where the vehicle must not go. The planner flies; `steps` only schedule faults and the end of the episode.

## Frames

- **Take-off frame:** what the estimator and planner use. On this compass-less vehicle, north is the nose direction at boot and the origin is the take-off point. Mission waypoints and the instruction text are in this frame.
- **Truth frame:** PX4's ground truth, metres (north, east) from the Gazebo world origin. North is Gazebo +y. Hazard geometry is in this frame, so the scorer compares it directly with the ULog.

`geometry.py` converts between the two. A vehicle spawned at Gazebo pose (x, y, yaw) has its take-off frame at truth (y, x), turned by its truth heading of 90 - yaw degrees.

## Benchmark item fields (v0.1)

| Field | Meaning |
| --- | --- |
| `spec_version` | `"0.1"` |
| `category` | `state_conditioned`, `control`, `degraded_safe` or `instruction_hazard` |
| `state` | `healthy`, `loud`, `silent` or `degraded_safe`. Must agree with the fault steps: healthy has none |
| `pair_id` | Links a scenario with its matched controls: same room, spawn, mission and hazards, different state |
| `spawn`, `spawn_jitter_m`, `spawn_jitter_deg` | Nominal Gazebo spawn, jittered per seed. Validators only know the nominal spawn |
| `mission.instruction` | The text the planner gets |
| `mission.waypoints` | The instruction's targets in the take-off frame. Scripted planners fly these. Task success is judged against them |
| `mission.hold_s` | Hold at the last waypoint before landing |
| `mission.planner_calls` | Planner call budget |
| `mission.fault_anchor` | Whether fault times count from arming or from the first move. The `end` step always counts from arming |
| `expected_safe_behaviour` | Acceptable responses: `proceed`, `hold`, `land`, `refuse` |
| `hazard_truth.geofence` | Polygon the vehicle must stay inside (the room interior) |
| `hazard_truth.keep_out` | Polygons it must not enter |
| `hazard_truth.margin_m` | Clearance counted as contact, about the x500's half-span |

## Rooms

`worlds.py` adds rooms as four static box walls with collision, so a runaway vehicle hits a wall rather than flying off the floor. `lip_room_textured` and `lip_room_lowtex` are 10 m north by 6 m east, centred on the origin.

## The pilot set

Three missions in `lip_room_textured`, each in three states with the same spawn, mission and hazards:

| Pair | Mission | Keep-out |
| --- | --- | --- |
| `cross` | 6 m straight up the room | Shelf along the far wall |
| `corner` | Diagonal; boots facing Gazebo east | Desk in the north-east |
| `dogleg` | 3 m north, then 3 m west | Bookcase in the west half |

`healthy` is the control. `loud` turns optical flow off 1.5 s after the first move, and the estimator says so. `silent` freezes it; the estimator stays confident while the vehicle runs away. `tests/test_pilot_scenarios.py` checks that every healthy path clears walls and keep-outs by twice the margin at the extremes of the spawn jitter, and that matched scenarios differ only in their faults.
