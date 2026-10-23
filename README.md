# U-HARNESS

A small, readable AI coding agent that runs in your terminal.

U-HARNESS hands a language model a set of file and shell tools and lets it work
on the directory you are standing in. It reads and searches files, writes and
edits them, runs commands, and asks your permission before it changes anything.
You can watch every step, approve or reject each action, and stop it with
Ctrl+C at any point.

It is a single Python file with one dependency. There is no framework, no daemon
and no server — you run it, it does the task, it exits.

Written by: Theo Uys

### Repository

Source code: [github.com/theouys/uharness](https://github.com/theouys/uharness)

Bug reports, questions and pull requests are welcome — open an issue on the
tracker and describe what you ran, what you expected and what happened instead.

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

- [Free by default, paid on demand](#free-by-default-paid-on-demand)
- [Prerequisites](#prerequisites)
- [Supported platforms](#supported-platforms)
- [Installation](#installation)
- [Getting an API key](#getting-an-api-key)
- [Quick start](#quick-start)
- [Command-line reference](#command-line-reference)
- [Approval and safety](#approval-and-safety)
- [Interactive mode](#interactive-mode)
- [Skills](#skills)
- [Environment variables](#environment-variables)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Self-test](#self-test)
- [Project layout](#project-layout)

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
  `CLICOLOR_FORCE` is unset and stdout is not a TTY.
- **Process killing.** Timeouts kill the whole process tree — via process
  groups on POSIX and `taskkill /T` on Windows.

Everything is pure Python standard library apart from `openai`. There is no
compiled code to build.

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

You can also skip the launcher and call the script directly, passing an explicit
workspace:

```bash
python3 /path/to/uharness.py --workspace ~/myproject "..."
```

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
| `-y`, `--yes` | off | Auto-approve writes, edits, deletes and command execution. |
| `--allow-dangerous` | off | Also permit commands matching the dangerous blocklist. |
| `--dry-run` | off | Report what would change; change nothing. |
| `--selftest` | | Run the offline tool-layer checks and exit. Needs no API key. |
| `-w`, `--workspace PATH` | current dir | The only directory the agent may touch. |
| `-m`, `--model NAME` | `deepseek-v4.1-flash:free` | Model to use. |
| `--max-turns N` | `20` | Model/tool round trips per request before wrapping up. |
| `--command-timeout S` | `120` | Default timeout for shell commands (max 600). |
| `--thinking` | off | Ask the provider to enable the model's reasoning mode. |
| `--usage` | off | Print a token-usage line after each request. |
| `--verbosity LEVEL` | `brief` | `quiet`, `brief` or `full` — see below. |
| `--no-banner` | off | Skip the startup logo. |
| `--log PATH` | off | Append the conversation to a file as JSON lines. |

### Verbosity

This controls **only what you see**. The model and `--log` always receive the
complete, untruncated result.

- `quiet` — the tool headline only. Command output is collapsed to a one-line
  summary. Warnings, errors and denials are still shown; the plumbing lines
  (context, usage, skill loading) are not.
- `brief` *(default)* — the same, plus the outcome of each call, e.g.
  `↳ Created app.py (412 bytes, 18 lines)`. A failing command still shows why.
- `full` — every result body, every line of command output.

Change it at runtime with `/verbose`, or set `HARNESS_VERBOSITY` in the
environment. `--verbosity` on the command line wins.

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
| `/save [name]` | Save the conversation to `.harness_sessions/`. Without a name, uses a timestamp. |
| `/open [name]` | List saved conversations and continue one. Accepts a number or a name. |
| `/sessions` | List saved conversations without opening one. |
| `/skills` | List the skill names available in this workspace. |
| `/approve [on\|off]` | Stop or start asking before write/edit/delete/execute. Blank toggles. |
| `/model [free\|paid]` | Show the model in use, or switch tier. Blank shows it. |
| `/context` | Show how much context the conversation is using. |
| `/verbose [full\|brief\|quiet]` | How much of each tool result is printed. |
| `/reset` | Forget the current conversation, keep the session alive. |
| `/help` | Show the command list. |
| `/exit` | Leave. |

Typing `exit` or `quit` (no slash) also leaves.

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

## Skills

A **skill** is a reusable pack of instructions you pull into a conversation on
demand, by typing `@{name}` in a prompt. It is plain Markdown, so you can write
one in a minute.

### Layout

Two forms are supported:

```
skills/
├── always-be-funny/
│   └── SKILL.md          # a folder, free to carry extra reference files
└── concise.md            # or a single file
```

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
you can add one and use it without restarting. Point at a different directory
with `HARNESS_SKILLS_DIR`.

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
| `HARNESS_SKILLS_DIR` | `skills` | Where skills are looked up. |
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
| `delete_file` | yes | Delete a file, or a directory with `recursive=true`. |
| `execute_command` | yes | Run a shell command with a timeout. |

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

---

## Self-test

The tool layer can be verified without an API key or any network access:

```bash
./uharness --selftest
```

It exercises path confinement, the file tools, ambiguous and CRLF-safe edits,
search, the delete guards, dry-run, command execution and timeouts, tree
killing, the approval logic, output bounding, the line editor's wrapping, history
compaction, saved sessions, skill loading and the agent loop (against a fake
client). Expected output:

```
selftest: 49/49 checks passed
All tool-layer checks passed (no network used).
```

A couple of checks are skipped when stdin is a TTY, because they need to observe
a non-interactive denial.

---

## Project layout

```
uharness                  Launcher script. Runs the agent in your cwd.
uharness.py               The entire agent. Single file, ~4,000 lines.
skills/                   Skill packs.
└── always-be-funny/
    └── SKILL.md          Example skill.
.harness_sessions/        Saved conversations (created on first /save).
.gitignore
README.md
```

`.harness_sessions/` is git-ignored. It holds your saved conversations in plain
JSON — they may contain code and paths you would rather not publish, so treat it
as private.

---

## License

Add your chosen licence here before publishing. If you want a permissive
starting point, MIT or Apache-2.0 are the usual picks for a tool like this.