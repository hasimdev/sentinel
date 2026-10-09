# Evals

## Triage eval (`evals/triage/`)

Checks the AI triage agent's diagnoses on 16 practice incidents with known right answers
(5 rollback, 7 investigate, 4 no action). See [triage/CASES.md](triage/CASES.md).

- **What runs:** the real triage agent (`orchestrator/triage.py`) and the real read-only
  tools (`mcp_server/tools.py`). Only the data is scripted (`world.py`): each incident is a
  small world of deploys, traffic, errors and logs, so every run sees the same evidence.
- **Marking** (`grader.py`): *Right call* = recommendation matches the answer key (automatic);
  *Right cause* = an AI marker (Claude Sonnet 5.5) compares the stated cause with the key;
  *Wrong rollback* = recommended rollback when it wasn't right (lower is better).
- **Safety gate:** the runner refuses to run after its code changes until a person runs
  `--approve-harness`, so scores can't silently shift.

```bash
python -m evals.triage.run --approve-harness   # a person, after reviewing changes
python -m evals.triage.run --reps 2            # 16 incidents x 2, about $6 and 3 minutes
```

Results: `.claude/hillclimb/triage/baseline/` (`results.jsonl`, `traces/`), and
`report.html` built with the Claude API skill's report builder.

**Baseline (9 Oct 2026, Claude Opus 5.5, effort medium):** right call 32/32, right cause
32/32, wrong rollbacks 0/32; median 18 s and 5 look-ups per diagnosis; $0.19 per run
(agent + marker).

**Limits:** the scripted metrics understand common query shapes, not all of PromQL. The set
is now at 100%, so it can't show further gains: add harder incidents before using it to
compare prompts or models.
