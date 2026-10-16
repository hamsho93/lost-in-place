# Pre-registered experiment design: archived versions

Every version of the experiment design that a freeze record in [SPEC.md](../../SPEC.md) has named, stored byte for byte and named by its SHA-256. The design lives outside this repo and changes only by dated amendment, so these copies are what the hashes in the freeze record can be checked against.

| SHA-256 | Amendments | Date | Status | Recorded in |
|---|---|---|---|---|
| [`44c1c04bbb3c5252ade679b8ac7c73fa43bd1e52e282d2ecbd4b3fd608abee86`](44c1c04bbb3c5252ade679b8ac7c73fa43bd1e52e282d2ecbd4b3fd608abee86.md) | A1–A10 | 2026-10-14 | **Freeze.** The design frozen at tag `freeze-v2`, under which E1 and E2 ran. Superseded by A11 | `freeze-v2`'s SPEC.md freeze record (commit `ca81dc2`) and the tag message |
| [`6d16e43065fdb5804fea1a484661e1f98e4a3c8ce0966e0bab0d8448da44e2c1`](6d16e43065fdb5804fea1a484661e1f98e4a3c8ce0966e0bab0d8448da44e2c1.md) | A1–A11 | 2026-10-15 | **Amendment A11** (E4 sized and specified), under which E4 ran. Current | SPEC.md's amendment log entry of 2026-10-15 and its freeze record (commit `8cda821`, [#62](https://github.com/hamsho93/lost-in-place/pull/62)) |

`freeze-v1` (withdrawn before any confirmatory run) recorded the design as `d55541e3…`; that version isn't archived here.

The files are exact copies, so they keep the design's front matter, and its relative links point to documents that aren't in this repo. Don't edit them: any change breaks the hash.

## Checking them

```bash
sha256sum docs/prereg/*.md
git show freeze-v2:SPEC.md | grep "Experiment design"
grep "Experiment design" SPEC.md
```

Each file's hash equals its name; `freeze-v2` names `44c1c04b…` and `main` names `6d16e430…`. The repo's own check passes for both:

```bash
IMG=sha256:e879c1ef7ea236a2814ed96d70ff830a73244763f4ac2be824b416f3864a790d
uv run lip freeze-record --design docs/prereg/6d16e43065fdb5804fea1a484661e1f98e4a3c8ce0966e0bab0d8448da44e2c1.md --image-digest $IMG --check
git worktree add /tmp/freeze-v2 freeze-v2 && cp docs/prereg/44c1c04b*.md /tmp/design-a10.md
(cd /tmp/freeze-v2 && uv run lip freeze-record --design /tmp/design-a10.md --image-digest $IMG --check)
```

`diff` between the two files shows only A11's edits: the §3 E4 prompt and E3-overlap bullets, the §5 O0 sentence, the §6 E4 row and "Running E4", the §11 E4 sentence, the §12 E4 bullet, the §13 timeline row and the A11 amendment-log row.

## Provenance of the A1–A10 copy

The working copy of the design was amended in place by A11, so no file with hash `44c1c04b…` survived. It was rebuilt on 2026-10-16 from the A1–A11 file by reverting exactly the A11 edits, as recorded when they were applied, and its hash matches the `freeze-v2` record byte for byte. No other text was changed.
