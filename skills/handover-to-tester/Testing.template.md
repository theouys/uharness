# Testing handover - <short task title>

- **Status:** READY FOR TESTING
- **Round:** 1
- **Updated:** <YYYY-MM-DD HH:MM>
- **Workspace:** <absolute path the work lives in>
- **One-line summary:** <what now exists that did not before>

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

Append-only. Newest entry last. Do not rewrite earlier entries.

### Round 1 - handover - <YYYY-MM-DD HH:MM>

- Built: <summary>.
- Self-tested: <commands run and their real results>.
- Known risk: <the part most likely to break, and where to look>.

<!-- The tester appends below this line, with its own status, findings and
     per-criterion results, then updates the Status field at the top of the
     file to PASSED or FAILED. -->
