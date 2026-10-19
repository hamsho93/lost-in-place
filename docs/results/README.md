# Results

Reports of confirmatory sweeps, one per freeze. Each report copies its numbers from the output of the pinned analysis script (`lip stats`, [docs/analysis.md](../analysis.md)) and states the frozen design, code, image and script hashes it was scored under. Pilots are not results; they are in [docs/pilot.md](../pilot.md).

| Freeze | Sweeps | Report |
|---|---|---|
| `freeze-v2` (2026-10-14) | E1 (1,512 episodes) and E2 (3,600 episodes), run 2026-10-15; the four Holm-corrected primaries | [freeze-v2.md](freeze-v2.md) |
| `freeze-v2` + amendment A11 (2026-10-15) | E4, the Claude Sonnet 4.6 frontier reference arm (1,200 episodes), run 2026-10-15; descriptive only, no test | [e4-claude.md](e4-claude.md) |
| `freeze-v3` (2026-10-17) | E5, the O-ladder wave: five LLM arms and the scripted control (11,601 scored episodes), run 2026-10-17 to 19; draft | [freeze-v3.md](freeze-v3.md) |

Every result is reported whatever its outcome (design §12), with each cell's counts and intervals, the harness failures and retries, the cost, and every disclosure made around unblinding.
