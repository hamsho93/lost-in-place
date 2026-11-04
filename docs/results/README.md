# Results

Reports of confirmatory sweeps, one per freeze. Each report copies its numbers from the output of the pinned analysis script (`lip stats`, [docs/analysis.md](../analysis.md)) and states the frozen design, code, image and script hashes it was scored under. Pilots are not results; they are in [docs/pilot.md](../pilot.md).

| Freeze | Sweeps | Report |
|---|---|---|
| `freeze-v2` (2026-10-14) | E1 (1,512 episodes) and E2 (3,600 episodes), run 2026-10-15; the four Holm-corrected primaries | [freeze-v2.md](freeze-v2.md) |
| `freeze-v2` + amendment A11 (2026-10-15) | E4, the Claude Sonnet 4.6 frontier reference arm (1,200 episodes), run 2026-10-15; descriptive only, no test | [e4-claude.md](e4-claude.md) |
| `freeze-v3` (2026-10-17) | E5, the O-ladder wave: five LLM arms and the scripted control (11,601 scored episodes), run 2026-10-17 to 19; draft | [freeze-v3.md](freeze-v3.md) |
| `freeze-v3` + E5b amendment (2026-10-21) | E5b, three later descriptive arms (Llama 3.3 70B, DeepSeek V3.2, Mistral Large 3; 6,120 LLM episodes + E5 reproducibility check), unblinded 2026-10-22; draft | [e5b.md](e5b.md) |

Mahmood's completed blind label sheet for the scorer-agreement spot-checks is in [label-spot-checks-2026-10-21.md](label-spot-checks-2026-10-21.md). It is discussed in freeze-v2 §6 and freeze-v3 §4.

[e5-attestation.md](e5-attestation.md) is a merge from Mahmood's account of an agent-written sentence on the E5 sign-off and unblind (freeze-v3 §6). It is not proof scores were unread, and it is silent on the sign-off about 17 hours before the record and on E5b.

Every result is reported whatever its outcome (design §12), with each cell's counts and intervals, the harness failures and retries, the cost, and every disclosure made around unblinding.
