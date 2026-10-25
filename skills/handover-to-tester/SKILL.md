---
name: handover to tester
description: Finish a development task, prove it actually works, then hand it to a separate testing agent by writing .harness_sessions/Testing.md - and stop working until the tester replies.
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
        |
        v
   tester reads, tests, appends
        |
        +-- status: PASSED  -> you report it and STOP. The job is done.
        |
        +-- status: FAILED  -> the log is your bug list. Fix, new round,
                               status: READY FOR TESTING again.
```

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
| `FAILED` | The tester's log entries are your task list. Fix them, self-test, then write the next round (`READY FOR TESTING`). |
| `BLOCKED` | Read why. Either remove the blocker, or ask the user - do not guess around a missing dependency. |
| `WITHDRAWN` | Your own earlier handover was pulled. Read your withdrawal note, then re-hand-over as a new round when it is genuinely ready. |

Reading this file is cheap. Skipping it means you either redo work the tester
already accepted or you build on top of a failure nobody read.

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

Use the template's sections in order:

- **Status / Round / Date / Summary** - the tester reads the first line to know
  whether it has work to do at all.
- **What changed** - every file with its path and one line on what it does now.
- **Acceptance criteria** - numbered, observable, independently checkable. "A
  POST to `/login` with a bad password returns 401 and logs one warning" is a
  criterion. "Login works" is a wish.
- **How to verify** - the exact commands and the expected output, so the tester
  reproduces your run instead of inventing a new one.
- **Not tested / out of scope** - the honest gaps, plus what you deliberately
  did not attempt.
- **Test log** - append-only, newest entry last.

Editing rules for the file:

- **Each round rewrites the handover block, never the log.** The status
  summary, criteria, verification steps and scope describe *this* round and get
  replaced. The log is history: append a `Round N handover` entry and leave every
  earlier entry - yours and the tester's - untouched.
- **Never edit the tester's entries.** If you disagree with a failure, add your
  own entry below it that says what you changed; do not rewrite their words.
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
changing a target the tester is about to test: say so, and either fold the
change in and re-hand-over, or wait. Never leave the file claiming to describe
code that has since moved.

## Withdrawing a handover

If you find, after writing, that the handover is wrong or the code is broken:

- **append** a `WITHDRAWN` log entry stating what is wrong and why;
- set `Status: WITHDRAWN` in the header;
- fix it, then re-hand-over as the next round.

Never quietly edit away a handover the tester may already be reading.

## Failure modes this skill exists to prevent

| Temptation | Why it bites |
|------------|--------------|
| Writing the handover before testing | The tester spends a session rediscovering your bug. |
| "Tests pass" with no command or output | Unverifiable claim; the tester has to redo it from zero. |
| Overwriting the log each round | Destroys the tester's findings and the audit trail of what was fixed. |
| Marking the file `PASSED` yourself | Ends the loop on an untested claim. Only the tester passes work. |
| Keeping to develop after handover | The tester tests a moving target, and the file lies about the code. |
| A handover per tiny edit | Log noise; the tester cannot tell what is actually up for review. |

## Chat summary shape

Four lines, no essay:

```
Built: <what now exists>. Files: <paths>.
Verified: <command> -> <actual result>.
Handover: .harness_sessions/Testing.md (Round <n>, READY FOR TESTING).
Stopping here - waiting on the tester.
```
