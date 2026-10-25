# U-HARNESS

A small, readable AI coding agent that runs in your terminal — or in a small
tkinter window, if you prefer.

U-HARNESS hands a language model a set of file and shell tools and lets it work
on the directory you are standing in. It reads and searches files, writes and
edits them, runs commands, and asks your permission before it changes anything.
You can watch every step, approve or reject each action, and stop it with
Ctrl+C at any point.

**It can play either side of a code review.** By default it is a **developer**:
it reads, writes and runs things to build what you asked for. Start it with
`--tester` and the same program becomes a **tester**: it reads a handover, tests
someone else's work, and reports what is broken — while being physically unable
to change the code it is judging. That makes a full two-agent loop possible from
one file:

```
developer            .harness_sessions/Testing.md            tester
uharness.py    ->    status: READY FOR TESTING       ->   uharness.py --tester
     ^                                                          |
     +----- status: FAILED / PASSED, with the findings <--------+
```

Each side writes the same plain-text file, so the handover between them survives
a crashed session, a different terminal, or a different agent entirely. See
[Developer or tester: two modes](#developer-or-tester-two-modes).

It is a single Python file with one dependency. There is no framework, no daemon
and no server — you run it, it does the task, it exits.

Written by: Theo Uys

### Repository

Source code: [github.com/theouys/uharness](https://github.com/theouys/uharness)

Bug reports, questions and pull requests are welcome — open an issue on the
tracker and describe what you ran, what you expected and what happened instead.

---

## Developer or tester: two modes

One file, two jobs. The flag decides which, and it changes three things: the
system prompt, the tool menu, and what the write tools will touch.

| | Developer (default) | Tester (`--tester`) |
|---|---|---|
| Job | Build or change the code | Check someone else's work and report defects |
| System prompt | "you are a coding assistant" | "you are a software tester" — never fix, only report |
| Tools offered | 8 | 7 — no `delete_file` |
| May write | Anything inside the workspace | **Only** `.harness_sessions/Testing.md` |
| May delete | Yes, with approval | No, not even the report |
| Startup logo | `U · H A R N E S S` | `U · T E S T E R` |

The tester's restriction is not a prompt instruction that the model could talk
itself out of — it is enforced in the tool layer. `resolve_write()` refuses
every path but the report and says where the finding belongs instead, so a
tester run cannot damage the code it is reviewing even if the model tries.

### The handover file

Both modes talk through `.harness_sessions/Testing.md`, a plain Markdown file
with a `Status:` line at the top and an append-only log underneath:

| Status | What the tester does |
|--------|----------------------|
| *(file missing)* | No handover — say so and stop. |
| `READY FOR TESTING` | Test it. The only status that means work. |
| `PASSED` | Accepted. Report it and stop. |
| `FAILED` | Not fixed yet. Report it and stop. |
| `BLOCKED` | Something must be unblocked first. |
| `WITHDRAWN` | The handover was pulled. |

The developer writes the criteria and the exact verification commands, self-tests
first, and stops. The tester reproduces those commands, probes the edges, and
appends a numbered defect list plus one of those statuses. The log is never
rewritten, so it doubles as the audit trail of what was fixed between rounds.

### Using it

Build something, then hand it over:

```bash
uharness --yes "Add a --json flag to cli.py and write a handover to .harness_sessions/Testing.md"
uharness --tester --yes "test the handover in .harness_sessions/Testing.md"
```

The second command comes back with findings; because the tester cannot edit the
code, fixing them is the developer's next turn:

```bash
uharness --yes "fix the defects in .harness_sessions/Testing.md round 2"
uharness --tester --yes
```

The tester is told to read the status line first, so a run against `PASSED` or
`FAILED` stops instead of inventing work. The status word belongs to the tester
alone — a developer that marks its own work `PASSED` has broken the loop.

Both modes work in the [desktop window](#desktop-window) too:
`python3 uharness-ui.py --tester` shows the same `U · T E S T E R` panel.

---

## Free by default, paid on demand

**You can run U-HARNESS for free.** Grab an API key from
[Token Harbor](https://tokenharbor.ai), drop it in an environment variable, and
the agent works out of the box — no subscription and no card required to get
started:

```bash
export TOKENHARBOR_API_KEY="thk_live_..."
```

That is the default configuration. The free tier drives DeepSeek's
`deepseek-v4.1-flash:free` model through Token Harbor's OpenAI-compatible
endpoint, and it is what the agent uses unless you tell it otherwise.

**Want the full-strength model? You can pay for it.** Swap in your own DeepSeek
key and the harness will talk to DeepSeek's endpoint directly, on their
`deepseek-flash` model:

```bash
export DEEPSEEK_API_KEY="sk-..."
```

So the deal is simple: **both tiers are DeepSeek** — the free Token Harbor route
costs nothing to try, and the paid DeepSeek route is there when you want more
horsepower. Set either key (or both), then switch between them on the fly inside
a session with `/model free` and `/model paid`. If both keys are present, the
free tier wins until you explicitly name a paid model.

See [Getting an API key](#getting-an-api-key) for the details, or
[Environment variables](#environment-variables) for every knob.

---

## Contents

- [Developer or tester: two modes](#developer-or-tester-two-modes)
- [Free by default, paid on demand](#free-by-default-paid-on-demand)
- [Prerequisites](#prerequisites)
- [Supported platforms](#supported-platforms)
- [Installation](#installation)
- [Getting an API key](#getting-an-api-key)
- [Quick start](#quick-start)
- [Getting help](#getting-help)
- [Command-line reference](#command-line-reference)
- [Approval and safety](#approval-and-safety)
- [Interactive mode](#interactive-mode)
- [Desktop window](#desktop-window)
- [Desktop launcher (Fedora / GNOME)](#desktop-launcher-fedora--gnome)
- [Skills](#skills)
- [Environment variables](#environment-variables)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Self-test](#self-test)
- [Project layout](#project-layout)
- [License](#license)

---

## Prerequisites

### Python 3.10 or newer

The `openai` package itself requires Python 3.10, so that is the floor.
Python 3.11, 3.12, 3.13 and 3.14 all work.

Check what you have:

```bash
python3 --version
```

If that prints anything below 3.10, install a newer Python first. On Debian or
Ubuntu:

```bash
sudo apt install python3 python3-pip
```

On Fedora:

```bash
sudo dnf install python3 python3-pip
```

On macOS with Homebrew:

```bash
brew install python@3.12
```

On Windows, use the installer from [python.org](https://www.python.org/downloads/)
and tick **"Add python.exe to PATH"** during setup.

### The `openai` package

Only one third-party dependency is needed:

```bash
pip install openai
```

If your system Python is "externally managed" (recent Debian, Ubuntu, Fedora and
Homebrew Python all are), install it in a virtual environment instead, or use
`pip install --user openai`.

### `pip` and a terminal

- `pip` comes with Python. On Debian/Ubuntu it is a separate package:
  `sudo apt install python3-pip`.
- You need a real terminal. Interactive mode reads keystrokes one at a time so
  that backspace, the arrow keys and Ctrl+U work; when input is piped in (a
  script, a pipe) it falls back to a simpler reader automatically.

### Outbound HTTPS

The agent talks to an OpenAI-compatible endpoint over HTTPS. It needs outbound
network access to that host. Behind a corporate proxy, set the usual
`HTTPS_PROXY` / `HTTP_PROXY` environment variables, which the `openai` library
honours.

---

## Supported platforms

| Platform | Status | Notes |
|----------|--------|-------|
| Linux | Fully supported | Tested on Fedora 44, Python 3.14 |
| macOS | Fully supported | Same POSIX code paths as Linux |
| Windows | Supported | Console input uses `msvcrt`; command timeouts use `taskkill /T` |
| WSL | Fully supported | Behaves as Linux |
| FreeBSD / OpenBSD | Expected to work | Untested; uses standard POSIX APIs |

The agent adjusts to its environment in three places:

- **Blocked or missing Unicode.** The logo, box-drawing characters and status
  glyphs fall back to plain ASCII when the terminal's encoding cannot print
  them, so it stays readable on a bare console.
- **Colour.** ANSI colour switches itself off when output is piped or
  redirected, and is disabled entirely by `NO_COLOR=1`, `TERM=dumb`, or when
  `CLICOLOR_FORCE` is unset and stdout is not a TTY. The model's final answer
  is the one block drawn in light blue, so it stands out from the green
  running commentary; the desktop window follows the same rule.
- **Process killing.** Timeouts kill the whole process tree — via process
  groups on POSIX and `taskkill /T` on Windows.

Everything is pure Python standard library apart from `openai`, plus `tkinter`
for the optional [desktop window](#desktop-window) — and that is stdlib too,
just not always installed. There is no compiled code to build.

---

## Installation

Clone the repository and run the launcher:

```bash
git clone <your-fork-url> uharness
cd uharness
pip install openai
./uharness --help
```

The `uharness` launcher runs the agent **from wherever you are standing**, not
from where the code lives. That is the whole point: `cd` into the project you
want the agent to work on, then run `uharness` from there.

To put it on your `PATH` so you can call it from any directory:

```bash
ln -s "$PWD/uharness" ~/.local/bin/uharness
```

The launcher follows symlinks, so this works. If `~/.local/bin` is not on your
`PATH`, either add it or use `/usr/local/bin` with `sudo`.

The launcher is a pure pass-through: every argument you give it — options, the
prompt, quoted words containing spaces, `--`, even things that look like flags —
reaches `uharness.py` untouched, in the same order, and the agent's exit status
is the launcher's exit status. So the command-line reference below applies
verbatim to `uharness`:

```bash
uharness -y --verbosity full "clean up utils.py and run the tests"
```

The only knob the launcher itself reads is `UHARNESS_PYTHON`, naming the
interpreter to use (default: `python3`, falling back to `python`):

```bash
UHARNESS_PYTHON=~/.venvs/agent/bin/python uharness --selftest
```

You can also skip the launcher and call the script directly, passing an explicit
workspace:

```bash
python3 /path/to/uharness.py --workspace ~/myproject "..."
```

If you would rather have a window than a prompt, `uharness-ui.py` beside it is
the same agent with a tkinter front-end — see [Desktop window](#desktop-window).
It needs `python3-tk`, which the terminal version does not.

---

## Desktop launcher (Fedora / GNOME)

`uharness-ui` is the command behind the desktop icon, and it is the same agent
with a window: prompt on top, feedback below, every slash command in the menu
bar. Fedora can launch it from the application grid:

```bash
./install-launcher.sh              # install
./install-launcher.sh --uninstall  # remove
```

That creates three things, all under `$HOME`, so no `sudo` is needed:

| Path | What it is |
|------|------------|
| `~/.local/bin/uharness-ui` | Symlink to `uharness-ui-launcher` (the command) |
| `~/.local/share/applications/uharness-ui.desktop` | The application-grid entry |
| `~/.local/share/icons/hicolor/scalable/apps/uharness-ui.svg` | The icon |

Re-running the installer is safe: the command is a *symlink* into this repo, so
edits here take effect immediately and only the icon and `.desktop` file are
copied. `desktop-file-validate` runs automatically when installed
(`sudo dnf install desktop-file-utils` to get it).

**Which folder it opens on.** A window started from the application grid
inherits the home directory as its working directory, which would silently make
your entire `$HOME` the agent's workspace. The launcher avoids that: it uses
`$UHARNESS_WORKSPACE` if set, otherwise the first of `~/Projects`, `~/projects`,
`~/repos`, `~/src`, `~/code` or `~/workspace` that exists, and only falls back
to `$HOME` as a last resort. An explicit `--workspace` always wins, and
*Conversation > Change workspace…* moves the window somewhere else without
restarting it.

**Fedora dependencies.** The window needs tkinter, which Fedora ships
separately:

```bash
sudo dnf install python3-tkinter
python3 -m pip install openai
```

The launcher pre-flights both before starting and names the missing package in
a dialog, so a grid launch cannot fail silently. Launched from a terminal it
prints the error there instead, and from the grid the details also land in
`~/.local/state/uharness-ui/launcher.log`.

**From the command line**, the same launcher takes every `uharness-ui.py`
option:

```bash
uharness-ui                             # window on the default workspace
uharness-ui --workspace ~/project        # ... on a folder you pick
uharness-ui "Summarise README.md"        # ask straight away
uharness-ui --verbosity full --usage     # same flags as the terminal agent
uharness-ui --help
```

In the window: Enter sends, Shift+Enter adds a line, Ctrl+↑/↓ walks the prompt
history, Ctrl+S saves the conversation, Ctrl+O opens one, Ctrl+Shift+Z undoes
the last request's file changes, Ctrl+L clears the feedback pane and Ctrl+Q
quits. The terminal "allow?" prompt becomes a dialog
with *Allow once*, *Deny* and *Always allow* - the dangerous-command blocklist,
`--dry-run` and auto-approval all still apply. The UI imports `uharness.py`
rather than copying it, so the tools, skills, saved sessions, autosave and
context compaction are exactly the terminal version's; the terminal version
does not need the UI at all.

---

## Getting an API key

The agent needs an API key for an OpenAI-compatible endpoint. It ships
configured for two tiers and can switch between them at runtime.

**Free tier (default)** — Token Harbor, model `deepseek-v4.1-flash:free`:

```bash
export TOKENHARBOR_API_KEY="thk_live_..."
```

**Paid tier** — DeepSeek's own endpoint:

```bash
export DEEPSEEK_API_KEY="sk-..."
```

Set one of them. If both are set, the free tier is used unless you name a paid
model. Add the `export` line to your `~/.bashrc` or `~/.zshrc` to make it
permanent.

On Windows PowerShell:

```powershell
$env:TOKENHARBOR_API_KEY = "thk_live_..."
```

If no key is set, the agent prints exactly which variable to export and exits
with status 2. It never stores or logs your key.

---

## Quick start

Assuming you have exported **either** key from
[Free by default, paid on demand](#free-by-default-paid-on-demand) — the free
Token Harbor key is enough to begin.

One-shot task, agent asks before each change:

```bash
cd ~/projects/myapp
uharness "Read the README and add a section about contributing"
```

Approve everything up front, for a task you trust:

```bash
uharness --yes "Rename every instance of getUser to fetch_user across the source tree"
```

See what it *would* do, changing nothing:

```bash
uharness --dry-run "Refactor utils.py to use pathlib"
```

Interactive session — you type prompts, it keeps the conversation going:

```bash
uharness
```

Inside an interactive session:

```
you> @{always-be-funny} summarise what this repository does
you> now write that summary to README.md
you> /save myapp-summary
you> /exit
```

---

## Getting help

Two doors, both offline and both instant:

| Where | What you get |
|-------|--------------|
| `uharness --help` | Every command-line option, the interactive command list with the aliases, the skills lookup rules, which environment variable holds each tier's key, and worked examples. |
| `/help` inside a session | Just the command panel — each command on one line with its argument hint and aliases — so it stays readable while a conversation is open. |

Neither contacts the network or needs an API key, so `uharness --help` works on
a fresh checkout before you have configured anything. A typo in a slash command
prints a warning naming the bad word and points back at `/help`; it is never
sent to the model.

The [desktop window](#desktop-window) takes the same flags, so
`python3 uharness-ui.py --help` prints the same reference with the window's key
bindings added.

```
╭─ commands ──────────────────────────────────────────────────────────────╮
│ /save [name]        save this conversation (blank: timestamp name)      │
│ /open [name]        list saved conversations and continue one           │
│ /sessions           list saved conversations (alias /ls)                │
│ /skills             list skill names (alias /skill)                     │
│ /approve [on|off]   ask before write/edit/delete/execute (alias /auto)  │
│ /model [free|paid]  show the model in use, or switch tier               │
│ /context            how much context the conversation uses (alias /ctx) │
│ /verbose [level]    quiet, brief or full (alias /verbosity)             │
│ /reset              forget the current conversation                     │
│ /help               this list (alias /?)                                │
│ /exit               leave (alias /quit; 'exit' and Ctrl+D too)          │
╰─────────────────────────────────────────────────────────────────────────╯
  unknown commands are warned about, never sent to the model
```

Start with `--tester` and two lines change: the `/approve` row reads "ask before
report writes and commands", and the panel reports 7 tools instead of 8.

---

## Command-line reference

```
uharness [options] [prompt ...]
```

The prompt is any number of words; quote it so the shell passes it through as
one argument. If you give no prompt, you drop straight into interactive mode.

### Options

| Option | Default | What it does |
|--------|---------|--------------|
| `-h`, `--help` | | Show the built-in help and exit. |
| prompt | *(interactive)* | The task to run. Omitted entirely, starts interactive mode. |
| `-i`, `--interactive` | off | Run the prompt (if any), then stay open for more. |
| `--tester` | off | Run as the tester half of the loop: read `.harness_sessions/Testing.md`, test what it describes, report defects there. The write tools accept that one path only and `delete_file` is not offered. See [two modes](#developer-or-tester-two-modes). |
| `-y`, `--yes` | off | Auto-approve writes, edits, deletes and command execution. Does **not** lift the dangerous-command blocklist. |
| `--allow-dangerous` | off | Also permit commands matching the dangerous blocklist. Needs approval as well. |
| `--dry-run` | off | Report what would change; change nothing. |
| `--selftest` | | Run the offline tool-layer checks and exit. Needs no API key. |
| `-w`, `--workspace PATH` | current dir | The only directory the agent may touch. |
| `-m`, `--model NAME` | `deepseek-v4.1-flash:free` | Model to use. Overrides the tier defaults from the environment. |
| `--max-turns N` | `20` | Model/tool round trips per request before the model is asked to wrap up. |
| `--command-timeout S` | `120` | Default timeout for shell commands (max 600). The model may ask for a different one per call. |
| `--thinking` | off | Ask the provider to enable the model's reasoning mode. |
| `--usage` | off | Print a token-usage line after each request. |
| `--verbosity LEVEL` | `brief` | `quiet`, `brief` or `full` — see below. |
| `--no-autosave` | off | Do not write the conversation to `.harness_sessions/`: no per-turn saves and no save on exit. |
| `--no-banner` | off | Skip the startup logo. |
| `--log PATH` | off | Append the conversation to a file as JSON lines. |

The same reference is available offline — `uharness --help` prints every option,
the interactive command list, the skills lookup rules and which key each tier
needs, without contacting the network.

### Verbosity

This controls **only what you see**. The model and `--log` always receive the
complete, untruncated result.

In both `quiet` and `brief` each tool call prints its headline and a one-line
outcome — `⚙ write_file`, then `↳ Created app.py (412 bytes, 18 lines)` — and
nothing else: no result bodies, no lines of command output. That is why they
feel almost identical. The one difference is the plumbing.

- `quiet` — headlines and outcomes only. Every `warn`, `error` and `denied`
  line still prints, but the chatter is gone: no context or token-usage lines,
  no "loading skill" notices, no session badge on the way out.
- `brief` *(default)* — the same one-line display, plus those plumbing lines:
  context size, token usage, skill loading, retries and the exit badge.
- `full` — every result body and every line of command output.

Change it at runtime with `/verbose`, or set `HARNESS_VERBOSITY` in the
environment. `--verbosity` on the command line wins. `/verbose` with no
argument prints the level in force.

### Examples

```bash
# Add a test file and run it, approving each step
uharness "Write tests for parse_args in cli.py and run them"

# Let it work unattended but see the full output afterwards
uharness --yes --verbosity full "Port the scripts to Python 3.12"

# Read-only investigation
uharness --verbosity full "Explain how the auth flow works"

# Cap a long job so it stops and summarises
uharness --yes --max-turns 40 "Update every dependency reference"

# Record the whole session for later reference
uharness --log session.jsonl "Investigate why the build fails"

# Play the other role: test a handover and report defects
uharness --tester --yes "test the handover in .harness_sessions/Testing.md"
```

---

## Approval and safety

By default the agent **asks before it does anything that changes state**:
writing a file, editing a file, appending, deleting, or running a command.
Read-only tools (`list_files`, `search_files`, `read_file`) never ask.

The prompt looks like this:

```
  ? Run command  pytest -q
  allow? [y/N] always to stop asking:
```

- `y` — allow this one action.
- `n` or Enter — deny it. The agent is told it was denied and can try another
  approach.
- `always` — allow this and every later action for the rest of the session.
- `/approve on` and `/approve off` also work here, so you can change your mind
  mid-run without leaving the loop.

### Defence in depth

Several things are blocked regardless of how you answer:

- **Workspace confinement.** Every path is resolved and checked against the
  workspace. `../../etc/passwd`, absolute paths outside the workspace, and
  symlink escapes are all refused. `delete_file` refuses the workspace root.
- **A dangerous-command blocklist.** Patterns like `rm -rf /`, `mkfs`, `dd`
  to a device, block-device redirection, `shutdown`/`reboot`, fork bombs,
  `chmod 777 /` and `curl ... | sh` are refused *before* you are even asked.
  `--allow-dangerous` lifts this, and `--yes` does **not** imply it — you need
  both.
- **Non-interactive safety.** If stdin is not a terminal and `--yes` was not
  given, write actions are denied rather than silently approved.
- **The tester fence.** With `--tester` only `.harness_sessions/Testing.md` may
  be written, and `delete_file` is not in the tool menu at all. `--yes` does not
  lift it: nothing else in the workspace can be created, changed or removed, so
  a reviewing run cannot damage what it is reviewing.

The blocklist is a guard rail, not a sandbox. It catches common catastrophes and
obvious mistakes; it cannot anticipate everything. **Run the agent in a
directory you are willing to have modified, and keep backups of anything
important.** Use `--dry-run` first when a task touches a lot of files.

---

## Interactive mode

Run `uharness` with no prompt, or with `-i`. The prompt line shows the active
tier above it:

```
  ● free deepseek-v4.1-flash:free
you> 
```

You get a proper line editor: backspace works, the arrow keys move and recall
history, Ctrl+U kills the line, Ctrl+W deletes a word, Ctrl+C stops the current
request, Ctrl+D (or `/exit`) leaves.

### Slash commands

| Command | What it does |
|---------|--------------|
| `/save [name]` | Save the conversation to `.harness_sessions/` under your own name. Without a name, uses a timestamp. Handy before a risky experiment. |
| `/open [name]` | List saved conversations and continue one. Accepts a number or a name. |
| `/sessions` | List saved conversations without opening one. Alias: `/ls`. |
| `/skills` | List the skill names available to this agent (workspace and bundled). Alias: `/skill`. |
| `/approve [on\|off]` | Stop or start asking before write/edit/delete/execute. Blank toggles. Alias: `/auto`. |
| `/model [free\|paid]` | Show the model in use, or switch tier. Blank shows it. |
| `/context` | Show how much context the conversation is using. Alias: `/ctx`. |
| `/verbose [full\|brief\|quiet]` | How much of each tool result is printed. Blank prints the level in force. Alias: `/verbosity`. |
| `/reset` | Forget the current conversation, keep the session alive. |
| `/undo [list]` | Put back the files the last request changed. A snapshot is taken automatically before every write, edit, append and delete; `list` shows what is still undoable. |
| `/help` | Show the command panel, aliases included. Alias: `/?`. |
| `/exit` | Leave. Aliases: `/quit`; typing `exit` or `quit` (no slash) also works. |

`/help` prints the same command panel as the tail of `uharness --help`, which
also documents every command-line option, the skills lookup rules and the API
keys. The greeting you see when a session opens lists the command names and
points at `/help` for the detail.

An unknown slash command is not sent to the model — it prints a warning naming
the offending word and points at `/help`, so a typo never costs you a request.

### Autosave, and picking up where you left off

You do not have to remember to save. Every conversation is written to
`.harness_sessions/` **as it happens**:

- the moment you send a prompt, before the model is even asked;
- after every turn, once the tool results are in;
- and once more on the way out, whether that is `/exit`, Ctrl+D or Ctrl+C.

So a crashed process, a closed terminal or a `kill` costs you at most the turn
in flight — not the session. One conversation is one file: the turn-by-turn
writes keep updating the same `chat-<timestamp>.json`, so autosaving a
100-turn session leaves one file behind, not a hundred.

`/save` still has a job: it forks the chat under a name you choose
(`/save refactor-attempt`), which is what you want before trying something you
may want to roll back. `/open` then restores any of them, and continues writing
back to that same file:

```
you> /sessions
  saved conversations
   1. refactor-attempt   14 messages   2026-10-23T21:14:02
   2. chat-20261023-210833   62 messages   2026-10-23T21:10:41
you> /open refactor-attempt
```

Autosave is on by default. `--no-autosave` turns off all of it — the per-turn
writes and the save on exit — for when you want a run that leaves no trace.

### Switching models on the fly

```
you> /model
  free  deepseek-v4.1-flash:free
you> /model paid
  ● ok  now using the paid model: deepseek-flash at https://api.deepseek.com
```

The switch takes effect on the next request. It only works if the matching API
key is set; if it is not, you get a clear error and the current model stays.

### Managing context

Long sessions grow. Once the conversation passes ~400,000 characters (tunable
via `HARNESS_MAX_HISTORY_CHARS`), the oldest tool results are replaced with a
short stub saying how much was dropped. Compaction stops once the conversation
is back under 70% of the threshold, and the 8 most recent tool results are never
dropped. Your messages and the model's replies are never touched — only bulky
tool output the model has already used. Ask for `/context` to see the breakdown:

```
╭─ context ─────────────────────────────────────╮
│ system     1,204 chars                        │
│ user       842 chars                          │
│ assistant  3,910 chars                        │
│ tool       18,442 chars                       │
│ messages   4                                  │
│ total      24,398 chars                       │
│ tokens     ~6,116 (estimated)                 │
│ budget     6% of 400,000 - 375,602 chars left │
╰───────────────────────────────────────────────╯
```

Character counts are exact. Token counts are an estimate — the harness does not
tokenise locally — so treat them as a rough guide.

---

## Desktop window

`uharness-ui.py` is the same agent in a tkinter window: the prompt box at the
top, everything the agent prints below it.

```bash
python3 uharness-ui.py                          # empty window
python3 uharness-ui.py "Summarise README.md"    # ask straight away
python3 uharness-ui.py --workspace ~/project --yes
```

It accepts every option `uharness.py` accepts — `--tester`, `--yes`,
`--dry-run`, `--thinking`, `--usage`, `--verbosity`, `-m`, `--max-turns`,
`--command-timeout`, `--log`, `--selftest` and the rest — and every slash
command works when typed into the prompt box, exactly as in the terminal. An
unknown slash command still only prints a warning and never reaches the model.
With `--tester` the window shows the `U · T E S T E R` panel and the same
report-only fence: the code stays read-only, and the report is the one writable
file.

The menu bar mirrors those commands, so nothing has to be memorised:

| Menu | What is in it |
|------|---------------|
| **File** | Save conversation (Ctrl+S), open one (Ctrl+O), list them, export the feedback pane, quit (Ctrl+Q) |
| **Agent** | Auto-approve, dry run, thinking mode, token usage, verbosity, free/paid model, show the model in use |
| **Conversation** | Forget the conversation, show context size, change workspace |
| **Skills** | List the skills available to this agent, refresh the skill index |
| **Tools** | Run the offline self-test |
| **View** | Clear the feedback pane (Ctrl+L) |
| **Help** | The command list, about this window |

Keys: `Enter` sends, `Shift+Enter` adds a line, `Ctrl+Up`/`Ctrl+Down` walk back
and forth through what you have typed, `Ctrl+S` saves, `Ctrl+O` opens, `Ctrl+L`
clears the pane, `Ctrl+Q` quits. While a request runs the **Stop** button
interrupts it, the same way Ctrl+C does in the terminal.

It is a skin, not a fork: it imports `uharness.py` and drives the same `Harness`
object, so the tools, skills, saved sessions, autosave, context compaction and
the dangerous-command blocklist are all the terminal version's. The one piece
replaced is the terminal `allow?` line, which becomes a modal dialog with
**Allow once**, **Deny** and **Always allow** (`Enter` allows, `Esc` denies).
**Always allow** throws the same switch as `/approve off` in the terminal — the
*Agent → Auto-approve tools* checkbox, which the window keeps in step with the
harness as you type commands. Autosave is the same store, so a conversation
begun in the window can be resumed with `/open` in the terminal, and the other
way round.

Needs tkinter and a display. macOS and the python.org Windows builds include it;
on Debian or Ubuntu install it with `sudo apt install python3-tk`. The terminal
version does not need tkinter, and `--selftest` and `--help` work in both regardless.

---

## Skills

A **skill** is a reusable pack of instructions you pull into a conversation on
demand, by typing `@{name}` in a prompt. It is plain Markdown, so you can write
one in a minute.

### Layout

Two forms are supported, and both can be mixed in the same folder:

```
skills/
├── always-be-funny/
│   └── SKILL.md          # a folder, free to carry extra reference files
└── concise.md            # or a single file
```

### Where skills are looked up

Without any configuration, **two** folders are searched, in this order:

| # | Folder | Why |
|---|--------|-----|
| 1 | `<workspace>/skills/` | Skills that belong to the project you are working in. |
| 2 | the `skills/` folder next to `uharness.py` | The packs the checkout ships with. |

Both are always searched, so you can keep a personal library in the repo and
project-specific skills in the project, and use either no matter which
directory you start the agent from:

```bash
cd ~/work/my-app
uharness "..."          # sees ~/work/my-app/skills and <repo>/skills
```

When two skills share a name, **the first folder wins**, so a project skill
shadows a bundled one of the same name. `/skills` lists the merged result.

To take over completely, set `HARNESS_SKILLS_DIR`. It **replaces** the two
default folders rather than adding to them, and accepts several paths
separated by `:` (`;` on Windows):

```bash
export HARNESS_SKILLS_DIR="$HOME/.config/uharness/skills:./skills"
```

A skills folder outside the workspace — a dotfile directory, say — is
supported: its skills can be read like any other, but the agent is not allowed
to edit or delete them, so a shared library cannot be damaged by a stray
`write_file`. A skill loaded from outside the workspace says so in the
conversation, and bundled reference files can be read with `read_file` and
copied into the workspace if they are needed there.

If a folder does not exist it is simply skipped; a workspace with no skills at
all is normal and the agent behaves exactly as before.

### Writing one

Frontmatter is optional. Without it, the folder (or file) name becomes the skill
name and the first meaningful line becomes the description.

```markdown
---
name: python-style
description: House rules for Python changes in this repo
---

# Python style

- Type hints on every public function.
- Prefer `pathlib` over `os.path`.
- Run `ruff check` before saying you are finished.
```

### Using one

Type the reference anywhere in your prompt:

```
you> @{python-style} add a helpers module
```

The skill's full text is inserted into the conversation **immediately before**
your request, so the model reads the instructions first. The system prompt
already lists every available skill with its description, so you can also just
ask "what skills do you have?", or run `/skills`.

Notes:

- Referencing the same skill twice in a session does not send it twice.
- Referencing a skill that does not exist produces a warning listing what *is*
  available, and the request continues without it.
- A skill adds conventions and workflows. It can never override the safety
  rules above, lift the dangerous-command blocklist, or outrank your own
  explicit instructions.
- If two loaded skills conflict, the most recently loaded one wins and the
  model is told to say so.
- Folder skills may ship extra files; the model is told their paths so it can
  read them with `read_file` if it needs them.

Skills are discovered when the agent starts and re-checked on every request, so
you can drop a new folder into either location and use it without restarting.
Run `/skills` to see what was found, or set `HARNESS_SKILLS_DIR` to point
somewhere else entirely.

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TOKENHARBOR_API_KEY` | *(required for free tier)* | API key for the free tier. |
| `DEEPSEEK_API_KEY` | *(required for paid tier)* | API key for the paid tier. |
| `TOKENHARBOR_MODEL` | `deepseek-v4.1-flash:free` | Free-tier model name. |
| `TOKENHARBOR_BASE_URL` | `https://tokenharbor.ai/v1` | Free-tier endpoint. |
| `DEEPSEEK_MODEL` | `deepseek-flash` | Paid-tier model name. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | Paid-tier endpoint. |
| `HARNESS_VERBOSITY` | `brief` | Default verbosity. |
| `HARNESS_MAX_HISTORY_CHARS` | `400000` | Compaction threshold. `0` disables it. |
| `HARNESS_SKILLS_DIR` | *(unset)* | Where skills are looked up. Unset: `<workspace>/skills` **and** the `skills/` folder next to `uharness.py`. Set: replaces both, `:`-separated (`;` on Windows) for several folders. |
| `HARNESS_INPUT` | *(auto)* | Set to `builtin` to use Python's plain `input()` instead of the arrow-key editor. |
| `NO_COLOR` | *(unset)* | Set to anything to disable ANSI colour. |
| `CLICOLOR_FORCE` | *(unset)* | Non-zero forces colour even when piped. |
| `TERM` | *(auto)* | `dumb` disables colour. |
| `COLUMNS` | *(auto)* | Overrides detected terminal width. |

Because both endpoints are configurable, you can point the harness at any
OpenAI-compatible server — a local model, an internal gateway, or another
provider — by setting the base URL and model variables.

---

## How it works

The agent is a tool-calling loop:

1. Your prompt (plus any skill text) is added to the conversation.
2. The conversation, the system prompt and the tool schemas are sent to the
   model.
3. The model either replies with text — done — or asks for one or more tool
   calls.
4. Each tool call is validated, approved if required, executed, and its result
   is appended to the conversation.
5. Repeat from step 2, up to `--max-turns` times. On hitting the limit the model
   is asked for a closing summary instead of another tool call.

### The tools

| Tool | Approval | Purpose |
|------|----------|---------|
| `list_files` | never | List a directory, optionally recursive. Skips noise directories. |
| `search_files` | never | Regex search across files, returning `path:line: text`. |
| `read_file` | never | Read a file, optionally a line range. |
| `edit_file` | yes | Replace an exact substring. Fails if the match is missing or ambiguous. |
| `write_file` | yes | Create or overwrite a file. |
| `append_file` | yes | Append to a file. |
| `delete_file` | yes | Delete a file, or a directory with `recursive=true`. **Not offered in `--tester` mode.** |
| `execute_command` | yes | Run a shell command with a timeout. |

In `--tester` mode the write tools are still listed but pointed at one path:
`write_file`, `edit_file` and `append_file` accept only
`.harness_sessions/Testing.md`, and the description the model reads says so. See
[two modes](#developer-or-tester-two-modes).

A few deliberate design choices:

- **`edit_file` over `write_file`.** Rewriting a whole file to change three
  lines risks silently losing content. `edit_file` requires the exact text and
  refuses ambiguous matches, so it cannot clobber the wrong place.
- **Output is bounded.** Tool results are capped (30,000 characters per result,
  60,000 per file read, 200 matching lines per search) and command output is
  buffered so a runaway build cannot exhaust memory. When something is cut, the
  model is told what was cut and how to ask for the rest.
- **The display never leaks.** Coloured and formatted output is for your eyes
  only. Tool results are constructed separately, so escape codes cannot reach
  the conversation history or the log file.

### The dangerous-command blocklist

Checked against every command before you are asked to approve it:

| Pattern | Reason |
|---------|--------|
| `rm` recursive on `/` | Recursive delete of the root filesystem |
| `mkfs`, `mkfs.ext4`, … | Formatting a filesystem |
| `dd ... of=/dev/...` | Writing raw data to a device |
| `> /dev/sd*`, `/dev/nvme*`, … | Overwriting a block device |
| `shutdown`, `reboot`, `poweroff`, `halt` | Shutting the machine down |
| `:(){ :|:& };:` | Fork bomb |
| `chmod 777 /` | Making the root directory world-writable |
| `curl ... \| sh`, `wget ... \| bash` | Piping a download into a shell |

---

## Troubleshooting

**`Missing dependency. Install it with: pip install openai`**
The `openai` package is not importable by the `python3` on your `PATH`. Install
it for *that* interpreter, and watch out for the externally-managed-environment
error — use a virtual environment or `pip install --user openai`.

**`Error: no API key is set.`**
Neither `TOKENHARBOR_API_KEY` nor `DEEPSEEK_API_KEY` is set. The message tells
you the exact line to add. Check with `echo $TOKENHARBOR_API_KEY` in the same
shell you run the agent from.

**It asks for permission on every single call.**
That is the default. Pass `--yes`, or answer `always` once at the prompt, or use
`/approve on`.

**"user denied permission to run the command" but you want it allowed.**
You likely typed `n`, or stdin is not a terminal. Rerun with `--yes`, or answer
`y`/`always` interactively.

**A command is blocked as dangerous.**
The blocklist matched. If it is a false positive, rerun with
`--allow-dangerous` — you need that *and* an approval.

**"path '...' is outside the workspace".**
The agent may only touch files under the workspace. `cd` to the right place, or
pass `-w /path/to/project`.

**`'...' is the code under test, and this is a tester run`.**
You are in `--tester` mode, which may write only
`.harness_sessions/Testing.md`. That is the whole point: findings go in the
report, never into the code. Drop `--tester` if you actually meant to edit
things — but then you are the developer, and the tester should be the one
judging the result.

**The tester does nothing and says the status is `PASSED` (or `FAILED`).**
Working as designed — those statuses mean "already handled", so it stops instead
of re-testing. Set the header back to `READY FOR TESTING` for a new round.

**Backspace prints `^?` or the arrow keys type `^[[A`.**
The terminal is not being driven in raw mode. Setting `HARNESS_INPUT=builtin`
gives Python's plain `input()`, which is uglier but more compatible.

**The logo is garbled or shows boxes.**
Your terminal's encoding is not UTF-8. The agent normally falls back to ASCII
automatically; force it with `export LANG=C` if it does not.

**Output has escape codes like `[32m` in a file or pipe.**
Set `NO_COLOR=1`. Colour should already switch off when stdout is not a TTY.

**A long command hit the timeout.**
Raise it on the command line (`--command-timeout 300`) or let the model pass a
larger `timeout` to `execute_command` (max 600). Note that a command waiting for
input that never comes will also time out.

**"You have reached the tool-call limit".**
The task needed more steps than `--max-turns`. Raise it, or split the task into
smaller requests.

**`ModuleNotFoundError: No module named 'tkinter'` from `uharness-ui.py`.**
The window needs tkinter; the terminal agent does not. On Debian or Ubuntu,
`sudo apt install python3-tk`; on Fedora, `sudo dnf install python3-tkinter`.
`python3 uharness.py --selftest` keeps working meanwhile.

**`couldn't connect to display ":0"` from `uharness-ui.py`.**
There is no graphical session to draw in — over SSH, either forward X
(`ssh -X`) or use the terminal version, which needs no display.

---

## Self-test

The tool layer can be verified without an API key or any network access:

```bash
./uharness --selftest
```

It exercises path confinement, the file tools, ambiguous and CRLF-safe edits,
search, the delete guards, dry-run, command execution and timeouts, tree
killing, the approval logic, output bounding, the line editor's wrapping, history
compaction, saved sessions, skill loading (from the workspace and from the
folder beside `uharness.py`), the tester fence (report-only writes, no
`delete_file`, the mode's prompt and logo) and the agent loop (against a fake
client).
Expected output:

```
selftest: 70/70 checks passed
All tool-layer checks passed (no network used).
```

The same checks run in either mode — `uharness --tester --selftest` prints the
same 70/70, because a tester run should be able to verify its own tool layer
before it judges anyone else's work.

The exact count moves as checks are added or removed, so treat it as a shape
rather than a number: what matters is that no `FAIL` line appears and the exit
status is 0. A couple of checks are skipped when stdin is a TTY, because they
need to observe a non-interactive denial.

---

## Project layout

```
uharness                  Launcher script. Runs the agent in your cwd.
uharness.py               The entire agent. Single file, ~5,650 lines. Coder
                          by default, tester with --tester.
uharness-ui.py            tkinter front-end: same agent, in a window.
uharness-ui-launcher      Shell launcher behind the desktop icon / PATH command.
uharness-ui.desktop       Application-grid entry (template; see the installer).
uharness-ui.svg           The icon, scalable.
install-launcher.sh       Installs the three above under ~/.local (and removes).
skills/                   Skill packs, searched from every workspace.
├── always-be-funny/
│   └── SKILL.md          Example skill.
├── handover-to-tester/
│   ├── SKILL.md          Developer side of the dev -> tester loop.
│   └── Testing.template.md  Shape for .harness_sessions/Testing.md.
└── test/
    └── SKILL.md          Example skill: inventory a folder into report.md.
.harness_sessions/        Autosaved conversations, written as you chat.
.gitignore
LICENSE                   Apache License 2.0 - the terms this project ships under.
README.md
```

The `skills/` folder here is the *bundled* one: it is searched in addition to
`<workspace>/skills`, whatever directory you launch from. See
[Where skills are looked up](#where-skills-are-looked-up).

`.harness_sessions/` is git-ignored. It holds your autosaved conversations in
plain JSON — they may contain code and paths you would rather not publish, so
treat it as private. Delete the folder whenever you like; the harness recreates
it on the next prompt.

---

## License

Copyright 2026 Theo Uys

Licensed under the **Apache License, Version 2.0** (the "License");
you may not use this project except in compliance with the License.
You may obtain a copy of the License at

<https://www.apache.org/licenses/LICENSE-2.0>

The full licence text is in the [LICENSE](LICENSE) file that ships with this
repository.

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Apache-2.0 is permissive: you may use, modify and redistribute this code,
including commercially and in closed-source projects, provided you keep the
copyright and licence notices and state any changes you made. It also grants
an explicit patent licence from contributors, which MIT does not.

### Contribution

Unless you explicitly state otherwise, any contribution intentionally
submitted for inclusion in this project by you shall be licensed as above,
without any additional terms or conditions — the usual Apache-2.0 inbound
rule (section 5).