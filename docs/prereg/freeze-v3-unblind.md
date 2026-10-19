# freeze-v3 unblinding

**Mahmood: "Unblind"** (Oct 19, 2026, 22:34 UTC, relayed by the coordinator.)

The marker that `lip stats --design v3` requires (`stats_v3.UNBLIND`; SPEC-v3 §6, design §2.5 and §12). Under his instruction, the agent commits it on his behalf. Design §12 has Mahmood commit it himself. Its conditions held at the time of commit:
- Every confirmatory arm had settled: claude-sonnet-4-6, claude-sonnet-5-5, gpt-5.6-sol, qwen3-4b-student and qwen3-4b-zero-shot each at 2,040 of 2,040 manifest entries. Claude Sonnet 4.6 has 39 missing episodes, each with all 3 attempts throttled and none resubmitted. The other arms have 0. The scripted control settled 960 + 480. No arm was deferred or dropped, and the dropped-arms row is `none`.
- The S-1 gate (G-S) passed on Oct 18, and no hard stop fired. Wave spend stayed within the $500 wave-level stop (amendment of 2026-10-18).
- The in-wave probes and the blind scorer-agreement labelling were complete. Only their counts, invalid rates and pooled agreement had been read.
- No outcome of any E5 arm had been read before this commit.

The analysis script is unchanged since `freeze-v3` (SHA-256 `15bdfb029d50421813a09c52d4a61f5cf364762e52ff4b82ce1a0c3ff5db9c28`, the record's analysis row). The one pinned analysis run follows this commit.
