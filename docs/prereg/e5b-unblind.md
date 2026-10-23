# E5b unblinding

**Mahmood: "unblind"** (2026-10-22, 21:35 America/New_York / 2026-10-23 01:35 UTC, in chat; relayed by the coordinator.)

The marker that `scripts/e5b.py analyse` requires (`UNBLIND`; SPEC-v3 E5b amendment, design §12). Under his instruction, the agent commits it on his behalf. Design §12 and sign-off choice 5 have Mahmood commit it himself; an own-account commit remains optional, as with E5's `freeze-v3-unblind.md`. Its conditions held at the time of commit:

- Every E5b arm had settled: `llama-3-3-70b`, `deepseek-v3-2` and `mistral-large-3` each at 2,040 of 2,040 manifest entries. The scripted control settled 960. No E5b arm was deferred or dropped. Admission kept every arm **descriptive** (Llama failed canary check 2; kept under sign-off choice 3).
- No hard stop fired. October's E5b stop stayed at $733.17 (baseline $553.17 + $180). The last submitter heartbeat before labeller closes read $613.01. Labeller windows closed at the plan rate ($17.70 and $10.20).
- The in-wave probes and the blind scorer-agreement labelling were complete. Only their counts, invalid rates and pooled agreement had been read. Labelling used `grok-4.7-high` for both passes (SPEC-v3 amendment of 2026-10-22, commit `c25ea14`); agreement vs the scorer was 2,347 / 2,347 with no disagreements.
- No outcome of any E5b arm had been read before this commit.

The analysis script is unchanged since `freeze-v3` (SHA-256 `15bdfb029d50421813a09c52d4a61f5cf364762e52ff4b82ce1a0c3ff5db9c28`, the record's analysis row). The one pinned `scripts/e5b.py analyse` run follows this commit on `origin/main`.
