# Testing handover - <short task title>

- **Status:** READY FOR TESTING
- **Round:** 1
- **Updated:** <YYYY-MM-DD HH:MM>
- **Workspace:** <absolute path the work lives in>
- **One-line summary:** <what now exists that did not before>

<!-- This file describes ONE round. Round 1 creates it; every later round
     replaces it wholesale with write_file. Never append a round on top of the
     tester's entry - carry what matters into the sections below instead.
     Omit the Previous round section entirely on Round 1. -->

## Previous round

Round <n-1> - **<PASSED | FAILED | BLOCKED>**, <p pass, q fail> criteria.

| Defect / finding | What I did about it |
|------------------|---------------------|
| <D1 - one line as the tester wrote it> | <fixed in `path/to/file.py:` - what changed> |
| <D2 - one line> | <fixed / disputed: why> |

Unchanged from that round: <what still stands, or "nothing">.

## What changed

| File | Change |
|------|--------|
| `<path/to/file>` | <one line: what it does now> |

## Acceptance criteria

Each line must be checkable by a tester who has never seen this code.

1. [ ] <Observable outcome - input, action, expected result.>
2. [ ] <Edge case - empty/missing/invalid input, expected behaviour.>
3. [ ] <Regression guard - the thing that already worked still works.>

## How to verify

Run from `<workspace>`.

```bash
# 1. <what this proves>
<exact command>
```

Expected:

```text
<the real output, copied from your own run>
```

```bash
# 2. <what this proves>
<exact command>
```

Expected: <result, or "exit code 0 and no output">

## Not tested / out of scope

- **NOT RUN - <reason>:** <what was not exercised, and why.>
- **Out of scope:** <what this round deliberately did not attempt.>

## Test log

This round only. The tester appends its result below and sets the Status line
at the top.

### Round <n> - handover - <YYYY-MM-DD HH:MM>

- Built: <summary>.
- Self-tested: <commands run and their real results>.
- Known risk: <the part most likely to break, and where to look>.

<!-- The tester appends below this line, with its own verdict, findings and
     per-criterion results, then updates the Status field at the top of the
     file to PASSED, FAILED or BLOCKED. The developer's next round replaces
     this whole file, carrying that verdict and its defect IDs into the
     Previous round section above. -->
