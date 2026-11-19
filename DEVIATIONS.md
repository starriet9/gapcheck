# DEVIATIONS

Departures from `PROTOCOL.md` after the protocol tag (policy: `PROTOCOL.md` sec. 12).

**None as of 2026-11-20.** The protocol is not frozen yet, so there is nothing to deviate from. This line is re-dated every time results or this file are updated.

## What counts

- A deviation: anything done differently from the tagged protocol that touches the test set, the systems, the prompts, the model or its settings, the scoring, the analysis, or what is reported.
- Not a deviation, logged in `RUNS.md` instead: a crash and the resumed run; use of the fallback beacon under the rule in sec. 13; raising the served context length before any model call so that every test case fits (sec. 5).
- Not a deviation: changes made before the tag. They are in the commit history of `SPEC.md` and `PROTOCOL.md`.

## Entry format

```
### D-<n>: <short title> (<date, UTC>)
- Protocol says: <section and sentence>
- Done instead: <what>
- Reason: <why>
- Decided: before the test seed | after the seed, before any model output | after model outputs were seen
- Affects: test set | model outputs | scoring | analysis | reporting
- Effect on results: <the protocol-specified result and the deviating result, side by side, where both can be computed; otherwise why not>
- Commit: <sha>
```

Entries are numbered in the order they are written and are never removed. A deviation decided after model outputs were seen is marked as such wherever its result is shown.
