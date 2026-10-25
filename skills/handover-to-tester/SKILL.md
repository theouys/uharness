---
name: handover to tester
description: Finish a development task, prove it actually works, then hand it to a separate testing agent by writing .harness_sessions/Testing.md - the whole file, replaced fresh each round - and stop working until the tester replies.
---

# Handover to Tester - Developer Side

You are the **developer** in a two-agent loop. A separate **tester** agent reads
`.harness_sessions/Testing.md` in the workspace, runs its own tests, and appends
the result to that same file. You never test in place of the tester, and the
tester never writes code.

```
you develop + self-test
        |
        v
.harness_sessions/Testing.md   status: READY FOR TESTING
        (the whole file, written fresh)
        |
        v
   tester reads, tests, appends its result
        |
        +-- status: PASSED  -> you report it and STOP. The job is done.
        |
        +-- status: FAILED  -> their findings are your bug list. Fix,
                               self-test, then OVERWRITE the file with
                               the next round: status READY FOR TESTING.
```

One file, two write moves, one per side:

- **You replace the file.** Every handover you write is the *whole* file -
  Round 1 creates it, every later round overwrites it. Nothing is appended, so
  the file always describes exactly one round and never stacks up.
- **The tester appends its result** under the log heading and changes the
  `Status` line. That entry is the thing you read before your next round, and
  the thing your overwrite replaces - so carry its essentials forward (Step 4).

## When this skill applies

Use it when the user asks you to build, change or fix something (feature, bug,
refactor, script, config) and expects the work to go out for testing.

Do not use it for questions, explanations, a quick read of a file, or work the
user explicitly wants you to keep iterating on locally with no handover.

## Step 1 - Read the current state before touching anything

If `<workspace>/.harness_sessions/Testing.md` exists, read it **first** and let
its `Status` decide your job:

| Status in the file | What you do |
|--------------------|-------------|
| file missing | New job. Develop, self-test, write the handover as Round 1. |
| `READY FOR TESTING` | Already handed over. Do **not** edit it and do **not** start new work. Tell the user it is waiting on the tester. |
| `PASSED` | Report it in chat and **stop**. No new development. Only start a new round if the user asks for a *new* change. |
| `FAILED` | The tester's findings are your task list. Read them, fix them, self-test, then **overwrite** the file with the next round (`READY FOR TESTING`) - carrying a short summary of what was reported. |
| `BLOCKED` | Read why. Either remove the blocker, or ask the user - do not guess around a missing dependency. |
| `WITHDRAWN` | Your own earlier handover was pulled. Read the note, then re-hand-over as a new round by **overwriting** the file when it is genuinely ready. |

Reading this file is cheap. Skipping it means you either redo work the tester
already accepted or you build on top of a failure nobody read.

When you overwrite a `FAILED` round, first take what you need out of the tester's
entry - verdict, defect IDs, the criteria that failed - and put it in the
`Previous round` section of the new handover. Your overwrite is what deletes
their words; the summary is how the fix stays traceable.

## Step 2 - Develop

Normal rules apply: smallest correct change, read files before editing, keep to
what was asked. Keep a list as you go of every file you touched and why -
you will need it in Step 4, and reconstructing it afterwards is how handovers
end up vague.

## Step 3 - Test it yourself, for real

The handover is a claim that the work runs. Make the claim true.

1. **Run the thing.** Execute the script, run the tests, hit the endpoint, run
   the linter or type check. Whatever proves the change works.
2. **Record the command and the actual output**, not your memory of the output.
   Copy the real lines, including the exit code when it matters.
3. **Try the neighbours**: empty input, wrong input, the rerun, the case the
   user described plus the one next door. Fix what breaks now, not in the
   tester's lap.
4. **If you could not run it**, say so: `NOT RUN - <reason>` (no network, no
   browser, missing credentials, needs a device). An honest gap is a finding;
   an invented pass is a lie that wastes the tester's whole session.
5. **Do not mark anything PASSED yourself.** That word belongs to the tester.

## Step 4 - Write the handover

Write `.harness_sessions/Testing.md` inside the workspace root. Create
`.harness_sessions/` if it does not exist - it is the same folder `/save` uses.
`Testing.template.md` next to this skill has the exact shape to copy.

**Write the whole file every round, with `write_file`. Never append a round onto
the tester's entry.** Round 1 creates the file; every later round replaces it.
The previous round is gone afterwards, so the new file must be complete on its
own.

Use the template's sections in order:

- **Status / Round / Date / Summary** - the tester reads the first line to know
  whether it has work to do at all.
- **Previous round** - from Round 2 on: the tester's verdict, the defect IDs it
  reported, and one line each on what you changed in response. This is the only
  thing that survives your overwrite, so it is what keeps the loop traceable.
- **What changed** - every file with its path and one line on what it does now.
- **Acceptance criteria** - numbered, observable, independently checkable. "A
  POST to `/login` with a bad password returns 401 and logs one warning" is a
  criterion. "Login works" is a wish.
- **How to verify** - the exact commands and the expected output, so the tester
  reproduces your run instead of inventing a new one.
- **Not tested / out of scope** - the honest gaps, plus what you deliberately
  did not attempt.
- **Test log** - this round's own `Round N handover` entry, nothing older. The
  tester appends its result under it.

Editing rules for the file:

- **Replace the file, never grow it.** Use `write_file` with the complete new
  handover. `append_file` is the tester's move, not yours: an appended second
  round leaves two blocks in one file and the tester cannot tell which one is
  current.
- **Read before you overwrite.** Never write the file while its status is
  `READY FOR TESTING` - the tester may be running. Overwrite only after you have
  the result in front of you and the fix is done and self-tested.
- **Carry the tester's findings forward in your own words.** Quote the verdict,
  keep their defect IDs (D1, D2, ...) so a fix maps to a report, and say what you
  did about each. Do not drop a defect silently - if you disagree, say why in the
  new round instead of deleting the disagreement.
- **Never write a round in the tester's voice.** You do not write verdicts, you
  do not mark criteria as passed on their behalf, and you never set `PASSED`.
- **Keep it readable in one screen or two.** Around 150 lines is the ceiling.
  Bind detail to code paths rather than pasting files in.
- **No secrets, tokens, passwords, API keys or personal data** in the file. It
  is plain text in a session folder and it will be read by another agent.

## Step 5 - Hand over and stop

After the file is written:

1. Give the user a short summary in chat: what was built, how you verified it,
   the path to `Testing.md`.
2. Say plainly that it is **ready for testing** and that you are stopping there.
3. **End the turn.** Do not keep developing, do not poll the file, do not run
   the tester's job, do not write a "PASSED" of your own. The tester is a
   different agent in a different session - the file is the only handshake.

If the user asks for more work while the status is `READY FOR TESTING`, they are
changing a target the tester is about to test: say so, and either fold the change
in and re-hand-over (overwrite the whole file with the updated round) or wait.
Never leave the file claiming to describe code that has since moved.

## Withdrawing a handover

If you find, after writing, that the handover is wrong or the code is broken:

- **overwrite** the file with the same handover, `Status: WITHDRAWN` in the
  header, and a short note saying what is wrong and why;
- keep the round number - the withdrawn file is still that round;
- fix it, then overwrite again with the next round, `READY FOR TESTING`.

Never quietly edit away a handover the tester may already be reading: the
`WITHDRAWN` status is the signal. The exception is the ordinary next round -
once the tester's result is in the file and you are re-handing-over, replacing
the file is exactly the intended move.

## Failure modes this skill exists to prevent

| Temptation | Why it bites |
|------------|--------------|
| Writing the handover before testing | The tester spends a session rediscovering your bug. |
| "Tests pass" with no command or output | Unverifiable claim; the tester has to redo it from zero. |
| Appending round 2 under the tester's entry | The file holds two rounds at once; the tester cannot tell which block is current. |
| Overwriting before reading the tester's entry | Their defect list is deleted and the same bugs come back with new numbers. |
| Overwriting while the status is `READY FOR TESTING` | You clobber a handover the tester is testing right now. |
| Marking the file `PASSED` yourself | Ends the loop on an untested claim. Only the tester passes work. |
| Keeping to develop after handover | The tester tests a moving target, and the file lies about the code. |
| A handover per tiny edit | Round numbers pile up; the tester cannot tell what is actually up for review. |

## Chat summary shape

Four lines, no essay:

```
Built: <what now exists>. Files: <paths>.
Verified: <command> -> <actual result>.
Handover: .harness_sessions/Testing.md (Round <n>, READY FOR TESTING).
Stopping here - waiting on the tester.
```
