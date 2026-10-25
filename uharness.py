#!/usr/bin/env python3
"""A small AI agent harness backed by the DeepSeek API via Token Harbor.

One script, two jobs.

As a coder (the default) the model can search, read, create, edit and delete
files and run shell commands inside a workspace. Tool calls are executed in
the order the model emits them, so a single request can touch several files
in sequence.

With --tester it becomes the other half of the two-agent loop. A developer
agent builds something, self-tests it and writes a handover to
.harness_sessions/Testing.md. The tester reads that handover, reads the code,
runs the tests and appends its findings to the same file. It fixes nothing:
the write tools accept one path only - the testing report - and every other
write is refused, so a tester run cannot damage the code it is reviewing.

    developer (uharness.py)  ->  .harness_sessions/Testing.md  ->  tester
                ^                                              (--tester)
                +---- reads the log, fixes, hands over the next round <--+
                                      (status: PASSED stops the loop)

Requirements:
    pip install openai
    export TOKENHARBOR_API_KEY="thk_live_..."   # previously: DEEPSEEK_API_KEY

Examples (coder):
    python3 uharness.py "Create a Flask hello-world app in app.py"
    python3 uharness.py "Read notes.txt and save a summary as summary.md"
    python3 uharness.py --yes "Make a script called backup.sh that tars ~/docs"
    python3 uharness.py --dry-run "Refactor utils.py to use pathlib"
    python3 uharness.py --no-banner "..."   # skip the U-HARNESS logo
    python3 uharness.py --thinking "..."     # enable the model's reasoning mode
    python3 uharness.py                  # interactive session
    python3 uharness.py --selftest       # offline check of the tool layer

Examples (tester):
    python3 uharness.py --tester --yes "test the handover in .harness_sessions/Testing.md"
    python3 uharness.py --tester --yes -w ~/project "verify round 2"
    python3 uharness.py --tester --dry-run "check src/importer.py and run its tests"
    python3 uharness.py --tester         # interactive session

In the interactive session, /save [name] stores the conversation in
.harness_sessions/ and /open brings one back to carry on where you stopped.
/model shows the model in use; /model free and /model paid switch between
the free Token Harbor model and the paid DeepSeek model.

Skills are reusable instruction packs. Type @{skill-name} anywhere in a prompt
and that skill's text is dropped into the context just before the request;
/skills lists what is available. Two folders are searched, the workspace's
skills/ directory first and then the skills/ folder beside this script, so the
bundled packs work from any project directory. HARNESS_SKILLS_DIR replaces both
with your own directory (or a colon-separated list).

The prompt reads its own keystrokes, so backspace, the arrow keys and Ctrl-U
work in any terminal (and editing keys never leak into the text). Setting
HARNESS_INPUT=builtin restores Python's plain input() instead.
"""

from __future__ import annotations

import argparse
import codecs
import collections.abc
import contextlib
import difflib
import io
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

try:
    from openai import (
        APIConnectionError,
        APIError,
        APIStatusError,
        OpenAI,
        RateLimitError,
    )
except ImportError:  # pragma: no cover
    print("Missing dependency. Install it with: pip install openai", file=sys.stderr)
    raise SystemExit(2)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# --- previous (paid DeepSeek) settings, kept for reference ---
# DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
# BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# --- current settings: Token Harbor free tier ---
DEFAULT_MODEL = os.environ.get("TOKENHARBOR_MODEL", "deepseek-v4.1-flash:free")
BASE_URL = os.environ.get("TOKENHARBOR_BASE_URL", "https://tokenharbor.ai/v1")

# The /model command swaps between these two profiles at runtime.
# "paid" is the original DeepSeek endpoint; "free" is the Token Harbor tier.
PAID_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
PAID_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
PAID_API_KEY_ENV = "DEEPSEEK_API_KEY"

FREE_MODEL = DEFAULT_MODEL
FREE_BASE_URL = BASE_URL
FREE_API_KEY_ENV = "TOKENHARBOR_API_KEY"

MAX_TOOL_OUTPUT = 30_000       # characters fed back to the model per tool call
MAX_FILE_CONTENT = 60_000      # characters read from one file
MAX_READ_LINES = 2_000
MAX_LIST_ENTRIES = 300
MAX_COMMAND_TIMEOUT = 600
MAX_COMMAND_CAPTURE = 200_000  # characters buffered from one command (x2 streams)
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_EDIT_DIFF = 4_000
MAX_LINE_PREVIEW = 200         # characters kept per search hit

# --- how much the console shows ------------------------------------------- #
# These shape the display only. The model always receives the full result;
# what is hidden here is hidden from your eyes alone.
VERBOSITY_LEVELS = ("quiet", "brief", "full")
VERBOSITY = os.environ.get("HARNESS_VERBOSITY", "brief").strip().lower()
if VERBOSITY not in VERBOSITY_LEVELS:
    VERBOSITY = "brief"

# brief = headlines only (what the agent is doing, not what came back).
# quiet = the same, minus the lines about plumbing (context/usage/wrapping up).
BRIEF_RESULTS = VERBOSITY in {"brief", "quiet"}
SHOW_NOTICES = VERBOSITY != "quiet"
SHOW_EXIT_BADGE = SHOW_NOTICES
# 'brief' is the default; /verbose full restores the old chatty behaviour.


def apply_verbosity(level: str) -> str:
    """Set the module display flags for `level`; returns the level actually set.

    Called silently at startup (--verbosity / HARNESS_VERBOSITY) and again by
    Harness.set_verbosity when /verbose is typed, so the rendering helpers and
    the per-call choice can never disagree.
    """
    global VERBOSITY, BRIEF_RESULTS, SHOW_NOTICES, SHOW_EXIT_BADGE
    chosen = (level or "").strip().lower()
    if chosen not in VERBOSITY_LEVELS:
        chosen = VERBOSITY
    VERBOSITY = chosen
    BRIEF_RESULTS = chosen in {"brief", "quiet"}
    SHOW_NOTICES = chosen != "quiet"
    SHOW_EXIT_BADGE = SHOW_NOTICES
    return chosen

# Conversation history is compacted once it grows past this many characters:
# old tool output is replaced with a stub so long sessions keep working.
MAX_HISTORY_CHARS = int(os.environ.get("HARNESS_MAX_HISTORY_CHARS", "400000"))
KEEP_RECENT_TOOL_RESULTS = 8

# /context reports how big the conversation has grown. Characters are counted
# exactly; tokens are estimated, because the harness does not tokenise locally.
CHARS_PER_TOKEN = 4      # rule of thumb for English prose and source code
TOKENS_PER_MESSAGE = 4   # role markers and separators the API adds per message

SESSION_DIR_NAME = ".harness_sessions"   # where /save writes conversations
MAX_SESSION_NAME = 60                    # characters kept from a session name
SESSION_PREVIEW_CHARS = 60               # first user line shown in /sessions

# Tester mode (--tester). The tester writes exactly one file: the report the
# developer agent reads. That single rule is what enforces "never change the
# code" - the write tools refuse every path but REPORT_PATH, delete_file is not
# offered at all - and it lives on the Harness instance (self.code_read_only),
# read from the TESTER default below, so run_selftest() can switch it per test.
REPORT_PATH = Path(SESSION_DIR_NAME) / "Testing.md"
TESTER = False   # set from --tester in main(); Harness(tester=...) overrides

# Skills: reusable instruction packs the user loads with @{skill-name}.
# Each skill is skills/<name>/SKILL.md, or a single skills/<name>.md file.
# Two folders are searched by default: <workspace>/skills and the skills/
# folder next to this script, so the same packs work from any directory.
HARNESS_SKILLS_DIR_ENV = "HARNESS_SKILLS_DIR"
SKILLS_DIR_NAME = "skills"     # the folder name used in both default locations
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_FILENAMES = ("SKILL.md", "skill.md")
MAX_SKILL_CHARS = 40_000       # characters read from one skill file
MAX_SKILL_NAME = 80            # characters kept from a referenced skill name
MAX_SKILL_INDEX = 2_000        # characters spent listing skills in the prompt
# A skill reference is "@{name}", at most MAX_SKILL_NAME characters, and never
# preceded by a word character (so an email address like "a@{x}" does not count).
SKILL_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_@])@\{([^{}\n]{1," + str(MAX_SKILL_NAME) + r"})\}"
)

# Directories that are noise for an agent looking for source files.
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    ".idea", ".vscode", ".DS_Store",
}

SYSTEM_PROMPT = """\
You are a careful, autonomous coding assistant running on the user's machine.
You can inspect, search, create, edit and delete files and run shell commands.

Rules:
1. Work inside the current workspace. Never try to escape it with paths like
   ../../ or absolute paths outside it.
2. Find things before you touch them: use search_files to locate code and
   list_files to see what exists. Read the relevant part of a file before
   editing it. Use read_file's start_line/end_line to page through long files.
3. Prefer edit_file for changes to existing files: pass an old_string that is
   long enough to be unique, plus the new_string. Only use write_file when
   creating a new file or genuinely replacing a file wholesale - rewriting a
   large file to change a few lines wastes effort and risks losing content.
4. When you create a file, give it a clear, descriptive name with the correct
   extension (for example: fetch_weather.py, backup_docs.sh, schema.sql).
   If the user already supplied a name, use that name exactly.
5. For multi-file work, call the tools once per file, one after another, so
   each file is written separately. Do not merge several files into one blob.
6. Keep shell commands non-destructive. Never run destructive commands
   (rm -rf /, mkfs, dd to a device, shutdown, etc.) unless the user explicitly
   asked for them. Use delete_file rather than rm so the user sees exactly
   what is removed.
7. Verify your work when you can: run the script, the tests, the linter or a
   syntax check, and report what the output actually said.
8. Tool output may be truncated. If something is missing, ask for a narrower
   range (read_file with line numbers, search_files with a tighter pattern)
   instead of guessing.
9. After using tools, tell the user briefly what you did, list the files you
   created or changed, and show any important command output.
10. If a tool returns an error, fix the problem and retry, or explain the
    blocker to the user. Do not invent results.
"""

TESTER_SYSTEM_PROMPT = """\
You are a software tester running on the user's machine. Another agent - the
developer - has already built or changed something and left you a handover.
Your job is to check that work, run it, and report what is wrong. It is not
your job to fix it: you never change the code you are testing.

Rules:
1. You never modify the code under test. Not the bug, not the test, not the
   formatting, not the docs, not the config. The only file you may write is
   `.harness_sessions/Testing.md`, and the file tools refuse every other
   path (delete_file is not offered to you at all). When you find a defect
   your output is a finding for the developer, never a patch.
2. Read `.harness_sessions/Testing.md` before anything else, and let its
   Status field decide the job:

   | Status in the file | What you do |
   |--------------------|-------------|
   | file missing | No handover. Say so and stop. |
   | READY FOR TESTING | Test it. This is the only status that gives you work. |
   | PASSED | Already tested and accepted. Report it and stop. |
   | FAILED | The developer has not fixed it yet. Say so and stop. |
   | BLOCKED | Something must be unblocked first. Say what and stop. |
   | WITHDRAWN | The handover was pulled. Say so and stop. |

3. Look before you run. Use list_files and search_files to find the code the
   handover names, then read_file the files it touches. Compare the summary
   and the acceptance criteria against the actual code - a handover can be
   wrong about itself, and that is a finding too. Stay inside the workspace:
   keep paths relative, and never trawl the rest of the filesystem with
   `find /` or absolute paths - the one exception is a path the handover
   itself names, where looking is how you check its claim. If the files it
   names are not here, that is the finding; say so, don't go hunting.
4. Reproduce the developer's own steps first: run the "How to verify"
   commands exactly as written. Then test the neighbours - empty input,
   invalid input, the rerun, the boundary the criteria hint at. Always
   record the command and the output you actually saw.
5. Judge behaviour, not claims. A criterion passes only when you observed
   the expected result yourself. "The code looks correct" is not a result,
   and neither is an unrun test.
6. Keep your commands read-only. Never run anything that rewrites the tree:
   no editors or sed -i, no `>` or tee into a source file, no cp/mv/rm, no
   git add/commit/checkout/stash/clean, no formatter or linter in fix mode
   (--fix, --write, gofmt -w), no package installs, no migrations against a
   real database. A test runner writing its own cache or temp files is fine.
   If something can only be proven by editing the code, do not edit it:
   record that criterion as NOT RUN - <reason>.
7. Report defects so the developer can act without guessing. Each defect
   gets a number (D1, D2, ...), a one-line description, where it lives (the
   file and line, or `environment` when nothing in the workspace is at
   fault), a severity, the exact command or input that triggers it, what you
   expected, and what actually happened.
8. If you cannot run something, write `NOT RUN - <reason>`. A missing
   dependency, no network, no credentials or a needed device are all honest
   findings. Never invent a result.
9. Never say PASSED unless every acceptance criterion was verified as
   passing and you found no defect. A criterion you could not test makes the
   round FAILED or BLOCKED - not PASSED.
10. Never rewrite the developer's handover text and never touch an entry that
    is already in the file - not even to correct a typo. You append one new
    entry and change one line (Status). Those two edits are your whole write:
    the developer's next round replaces the file and quotes your verdict back
    in its own handover, so say everything the fix needs in this entry.
11. Keep secrets out of the file: no tokens, passwords, API keys or personal
    data. Quote the minimum needed to reproduce a problem.
12. Tool output may be truncated. If something is missing, ask for a narrower
    range (read_file with start_line/end_line, search_files with a tighter
    pattern) instead of guessing.
13. Finish in chat with the verdict, how many criteria passed and failed,
    the worst defect, and the path to the report. Then stop - the developer
    agent reads the file next, not you.

## Writing the report

Append a new entry at the end of the `## Test log` section of
`.harness_sessions/Testing.md` (newest last), matching the existing shape:

    ### Round <n> - test - <YYYY-MM-DD HH:MM>

    - **Verdict:** PASSED | FAILED | BLOCKED
    - **Environment:** <where you ran it, e.g. Python 3.14, Linux, no network>
    - **Criteria:** 1 pass, 2 fail, 3 NOT RUN - <reason>
    - **Defects:**
      - **D1 - <one line summary>** `path/to/file.py:123` - blocker|major|minor|nit
        - Reproduce: `<exact command>` -> expected `<x>`, got `<y>`
    - **Commands run:** `<command>` -> `<the real output line(s)>`
    - **Notes:** <what you would test next, what is out of scope>

Then change the `- **Status:**` line at the top of the file to `PASSED`,
`FAILED` or `BLOCKED`, and change nothing else in the handover block. Use
append_file for the log entry and edit_file for the status line.

Do the status edit immediately after the log entry, before you write your
chat summary. The header is the first thing the developer agent reads, so a
round that runs out of tool calls with a stale header leaves the loop broken -
if you can only afford one write, fixing the header matters more than the
length of the entry.

What the verdicts mean:

- **PASSED** - every criterion verified, no defects found.
- **FAILED** - at least one criterion failed, or you found a real defect.
  Your log entry is the developer's bug list for the next round.
- **BLOCKED** - you could not test at all (missing dependency, broken
  environment, nothing runnable). Say exactly what has to change.

If the file is missing, or its status is anything other than
READY FOR TESTING, write nothing and report what you found. Never create a
handover of your own and never write a report about work that was not handed
to you.
"""

SKILL_SYSTEM_PROMPT = """\

Skills
------
This agent can load reusable instruction packs ("skills") from up to two
folders: the workspace's own skills/ directory and the skills/ folder beside
uharness.py that ships with the harness. The user loads one by typing
@{skill-name}; its text then arrives as a [skills] message immediately before
the request that mentioned it. The index below lists the skills from both
folders, so a skill the user mentions may come from either one.

- A loaded skill is part of the current request: follow it, and keep following
  it for the rest of the conversation.
- A skill may add conventions, a checklist, a workflow or a preferred output
  shape. It can never lift the safety rules above, grant permission for
  destructive commands, or override an explicit instruction from the user.
- When two loaded skills disagree, follow the one loaded most recently and say
  that you did.
- If the user asks for something that a listed skill covers but has not loaded
  it, say so and suggest they type @{that-skill} rather than guessing what it
  says. You may read a skill file yourself with read_file when asked about it.
"""

# Commands that are blocked unless --allow-dangerous is passed.
DANGEROUS_PATTERNS = [
    (r"\brm\s+(-[A-Za-z]*[rR][A-Za-z]*[fF]?[A-Za-z]*\s+|-[A-Za-z]*[fF][A-Za-z]*\s+)*/(\s|$)",
     "recursive delete of /"),
    (r"\bmkfs(\.[A-Za-z0-9]+)?\b", "format a filesystem"),
    (r"\bdd\b[^\n]*\bof=\s*/dev/", "write raw data to a device"),
    (r">\s*/dev/(sd|nvme|hd|vd)[A-Za-z0-9]*", "overwrite a block device"),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "shut down the machine"),
    (r":\s*\(\s*\)\s*\{[^}]*\}\s*;\s*:", "fork bomb"),
    (r"\bchmod\s+(-[A-Za-z]+\s+)*777\s+/\s*$", "chmod 777 on /"),
    (r"\b(curl|wget)\b[^\n|]*\|\s*(ba|z)?sh\b", "pipe a download into a shell"),
]
DANGEROUS_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in DANGEROUS_PATTERNS]


# --------------------------------------------------------------------------- #
# Tool definitions (OpenAI / DeepSeek function-calling schema)
# --------------------------------------------------------------------------- #

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and directories inside the workspace. Noise "
                "directories (.git, node_modules, __pycache__, ...) are skipped "
                "in recursive listings. Use this to discover what already "
                "exists before creating or editing files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory relative to the workspace. Defaults to '.'.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Also list subdirectories (default false).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search file contents with a Python regular expression and "
                "return matches as 'path:line: text'. Far cheaper than reading "
                "whole files when you are looking for where something is "
                "defined or used."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Python regex, e.g. 'def main' or 'TODO|FIXME'. "
                            "Escape regex metacharacters for literal text."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search (default '.').",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Only filenames matching this shell-style pattern, e.g. '*.py'.",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Match case-sensitively (default true).",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"Stop after this many matching lines (default {MAX_SEARCH_RESULTS}).",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "File path relative to the workspace.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to read, 1-based (optional).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to read, inclusive (optional).",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact substring in an existing file. This is the "
                "preferred way to change an existing file: it cannot silently "
                "drop the rest of the content the way a full rewrite can. "
                "old_string must match exactly, including indentation; include "
                "a few surrounding lines so it is unique. Fails without "
                "changing anything if old_string is missing or ambiguous."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "File path relative to the workspace.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact existing text to replace. Must not be empty.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text. Use '' to delete old_string.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence (default false; ambiguous matches fail).",
                    },
                },
                "required": ["filename", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a new file or overwrite an existing one with the given "
                "content. Parent directories are created automatically. Call "
                "this once per file when creating multiple files. Prefer "
                "edit_file for small changes to existing files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Target path relative to the workspace, including extension.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full text to write to the file.",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append text to the end of a workspace file, creating it if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "File path relative to the workspace."},
                    "content": {"type": "string", "description": "Text to append."},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Delete a file, or a directory with recursive=true, inside the "
                "workspace. Requires user approval. Refuses to touch the "
                "workspace root or anything outside it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "File or directory to remove, relative to the workspace.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Required to remove a non-empty directory (default false).",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": (
                "Run a shell command in the workspace and return its exit code, "
                "stdout and stderr. Output is streamed while the command runs "
                "and killed on timeout. Use this to run scripts, tests, "
                "linters or inspections. Prefer non-destructive commands."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."},
                    "timeout": {
                        "type": "integer",
                        "description": f"Timeout in seconds (default 120, max {MAX_COMMAND_TIMEOUT}).",
                    },
                },
                "required": ["command"],
            },
        },
    },
]

# In tester mode the same tools are offered, with two differences: delete_file
# is left out entirely, and the descriptions say where writes may go. The model
# reads these descriptions before it decides what to do, so telling it "only the
# report" up front is cheaper than letting it discover the refusal afterwards.
TESTER_TOOL_DESCRIPTIONS: dict[str, str] = {
    "edit_file": (
        "Replace an exact substring in the testing report "
        f"({REPORT_PATH.as_posix()}). old_string must match exactly, "
        "including indentation; include a few surrounding lines so it is "
        "unique. Fails without changing anything if old_string is missing or "
        "ambiguous. A tester run may write no other file, so every other path "
        "is refused."
    ),
    "write_file": (
        f"Create or overwrite the testing report ({REPORT_PATH.as_posix()}) "
        "with the given content. Parent directories are created automatically. "
        "A tester run may write no other file - the code under test is off "
        "limits and every other path is refused."
    ),
    "append_file": (
        "Append text to the end of the testing report "
        f"({REPORT_PATH.as_posix()}), creating it if needed. This is how a "
        "test-log entry is added. A tester run may write no other file, so "
        "every other path is refused."
    ),
    "execute_command": (
        "Run a shell command in the workspace and return its exit code, "
        "stdout and stderr. Output is streamed while the command runs and "
        "killed on timeout. Use this to run the handover's tests, linters and "
        "inspections - read-only commands only: anything that rewrites the "
        "tree is out of bounds for a tester."
    ),
}


def tools_for(tester: bool) -> list[dict[str, Any]]:
    """The tool menu for the current mode.

    The coder gets TOOLS as written. The tester gets the same list minus
    delete_file, with the write tools described as report-only. Copies are
    shallow: only the description changes, and the shared parameter schemas
    are never mutated.
    """
    if not tester:
        return TOOLS
    menu: list[dict[str, Any]] = []
    for tool in TOOLS:
        function = tool["function"]
        name = function["name"]
        if name == "delete_file":
            continue
        override = TESTER_TOOL_DESCRIPTIONS.get(name)
        if override is not None:
            function = {**function, "description": override}
        menu.append({**tool, "function": function})
    return menu


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class ToolError(Exception):
    """A recoverable tool failure that is reported back to the model."""


def clip(text: str, limit: int, keep: str = "both") -> str:
    """Trim text to `limit` characters, keeping the head, the tail, or both.

    Command output usually matters most at the *end* (that is where the
    traceback or test summary is), while file content matters at the start -
    hence the choice.
    """
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    if keep == "head":
        return text[:limit] + f"\n... [{dropped} more characters truncated]"
    if keep == "tail":
        return f"... [{dropped} earlier characters truncated]\n" + text[-limit:]
    head = int(limit * 0.6)
    tail = limit - head
    return (
        text[:head]
        + f"\n... [{len(text) - head - tail} characters truncated] ...\n"
        + text[-tail:]
    )


def format_result(stdout: str, stderr: str, exit_code: int) -> str:
    parts = [f"exit_code: {exit_code}"]
    parts.append("stdout:\n" + (stdout.rstrip() if stdout.strip() else "(empty)"))
    if stderr.strip():
        parts.append("stderr:\n" + stderr.rstrip())
    return clip("\n".join(parts), MAX_TOOL_OUTPUT)


def short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "..."


def make_diff(before: str, after: str, label: str) -> str:
    """A compact unified diff, used to confirm an edit to the model."""
    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
            n=2,
            lineterm="",
        )
    )
    if not diff:
        return "(no textual change)"
    return clip("\n".join(diff), MAX_EDIT_DIFF)


class BoundedBuffer:
    """Collects streamed text, keeping only the most recent `limit` characters.

    Keeps memory flat when a command produces megabytes of output, and records
    how much was dropped so the model knows the view is partial.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._chunks: list[str] = []
        self._size = 0
        self.dropped = 0

    def add(self, text: str) -> None:
        self._chunks.append(text)
        self._size += len(text)
        while self._size > self.limit and len(self._chunks) > 1:
            removed = self._chunks.pop(0)
            self._size -= len(removed)
            self.dropped += len(removed)

    def value(self) -> str:
        body = "".join(self._chunks)
        if self.dropped:
            return f"... [{self.dropped} earlier characters dropped]\n{body}"
        return body


# --------------------------------------------------------------------------- #
# Output presentation
# --------------------------------------------------------------------------- #
#
# Everything here affects only what a human reads. Tool results are built by
# the tool functions themselves and never pass through these helpers, so
# escape codes cannot leak into the conversation history or the --log file.
# Colour is opt-out (NO_COLOR, CLICOLOR_FORCE, TERM=dumb) and auto-disabled
# when output is piped or redirected, so logs and test captures stay plain.

MAX_RESULT_LINES = 16          # result lines shown before "… +N more lines"
RESULT_INDENT = "    "
# A tool argument is shown in full and folded onto as many rows as it needs,
# because a command you cannot read is a command you cannot approve.
# ARG_MAX_ROWS is what bounds the block in practice: one argument gets at most
# that many rows and the last one ends in an ellipsis if more was cut.
# ARG_VALUE_CHARS is only a backstop against measuring and folding megabytes of
# text for a single argument, so it sits far above what ARG_MAX_ROWS can show.
ARG_VALUE_CHARS = 2000
ARG_MAX_ROWS = 6


def _env_int(name: str, default: int) -> int:
    """A positive integer from the environment, else `default`."""
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# How wide the transcript is drawn, in characters. Everything the harness
# prints - model prose, tool calls, tool results, notices - is folded to fit
# this many columns, so output reads the same on a 200-column terminal as on an
# 80-column one. 80 is the classic width: comfortable to read, and it leaves
# room for two panes side by side. Change the number here (or set the
# HARNESS_WIDTH environment variable) and every block follows; nothing else in
# the file needs editing.
OUTPUT_WIDTH = _env_int("HARNESS_WIDTH", 80)

_RESET = "\x1b[0m"

# The grey behind the "run" band. The 16-colour palette only offers two greys -
# 47 (light) and 100 (dark) - with nothing in between, so the band takes its
# shade from the 256-colour ramp, where 250 down to 244 are the steps between
# them. Lower this number for a darker bar, raise it for a lighter one; the
# text on it stays black, which holds up down to about 240.
BAND_GREY = 248

_STYLES = {
    "bold": "1", "dim": "2", "italic": "3", "underline": "4",
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "grey": "90", "black": "30",
    "bright_red": "91", "bright_green": "92", "bright_yellow": "93",
    "bright_blue": "94", "bright_magenta": "95", "bright_cyan": "96",
    # Backgrounds. The band is a mid grey with black text on top, so the
    # command stays readable whatever the terminal's own foreground colour
    # happens to be. "bg_light_grey" is the fallback for terminals that cannot
    # show the 256-colour ramp: the old, lighter bar rather than none.
    "bg_grey": f"48;5;{BAND_GREY}",
    "bg_light_grey": "47",
    # The final answer is drawn in this one: bright blue reads as light blue
    # on every terminal that supports the bright range, and it is the only
    # colour the transcript uses for model prose, so the answer stands out.
    "light_blue": "94",
}

ANSWER_COLOUR = "light_blue"   # the model's final answer, not mid-turn prose

_UNICODE_GLYPHS = {
    "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│",
    "dot": "●", "gear": "⚙", "result": "↳", "arrow": "▸", "warn": "▲",
    "error": "✖", "retry": "↻", "bullet": "•", "ok": "✔", "fail": "✖",
    "ellipsis": "…", "prompt": "❯",
}
_ASCII_GLYPHS = {
    "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
    "dot": "*", "gear": ">", "result": "->", "arrow": ">", "warn": "!",
    "error": "x", "retry": "~", "bullet": "-", "ok": "+", "fail": "x",
    "ellipsis": "...", "prompt": ">",
}
_glyph_cache: dict[str, dict[str, str]] = {}

_NOTICE: dict[str, tuple[str, str, str]] = {
    "run": ("arrow", "yellow", "run"),
    "warn": ("warn", "yellow", "warn"),
    "error": ("error", "red", "error"),
    "deny": ("error", "red", "denied"),
    "retry": ("retry", "yellow", "retry"),
    "context": ("bullet", "grey", "context"),
    "usage": ("bullet", "grey", "usage"),
    "skill": ("gear", "magenta", "skill"),
    "ok": ("ok", "green", ""),
    "info": ("bullet", "grey", ""),
}


def supports_color(stream: Any = None) -> bool:
    """True when ANSI colour should be written to `stream`."""
    stream = sys.stdout if stream is None else stream
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("CLICOLOR_FORCE", "0") not in ("", "0"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def supports_256_colors(stream: Any = None) -> bool:
    """True when the terminal understands the 256-colour palette.

    Only needed for shades the 16-colour palette cannot name - the band grey,
    for one. TERM says so for the great majority of terminals; COLORTERM does
    for the truecolour ones, which all speak 256 as well. Anything else (a
    Linux console, an old xterm, a detached run) is assumed to have just the
    sixteen, and the callers fall back to a colour from that range.
    """
    if not supports_color(stream):
        return False
    if "256color" in os.environ.get("TERM", ""):
        return True
    colourterm = os.environ.get("COLORTERM", "").lower()
    return colourterm in {"truecolor", "24bit"}


def band_background(stream: Any = None) -> str:
    """The _STYLES key for the "run" band's background.

    Mid grey where the palette allows it, the old light grey where it does
    not; black text sits on the band either way.
    """
    return "bg_grey" if supports_256_colors(stream) else "bg_light_grey"


def encodable(text: str, stream: Any = None) -> bool:
    """True when `text` can be written to `stream` without a Unicode error."""
    stream = sys.stdout if stream is None else stream
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def glyphs(stream: Any = None) -> dict[str, str]:
    """Box-drawing characters the stream's encoding can actually print."""
    stream = sys.stdout if stream is None else stream
    encoding = getattr(stream, "encoding", None) or "utf-8"
    cached = _glyph_cache.get(encoding)
    if cached is not None:
        return cached
    if encodable("".join(_UNICODE_GLYPHS.values()), stream):
        chosen = _UNICODE_GLYPHS
    else:
        chosen = _ASCII_GLYPHS
    _glyph_cache[encoding] = chosen
    return chosen


def paint(text: str, *styles: str, stream: Any = None) -> str:
    """Wrap `text` in ANSI styles, or return it untouched when colour is off."""
    if not text or not styles or not supports_color(stream):
        return text
    prefix = "".join(f"\x1b[{_STYLES[s]}m" for s in styles if s in _STYLES)
    return f"{prefix}{text}{_RESET}"


def begin_answer(stream: Any = None) -> None:
    """Mark the block that follows as the final answer, where the sink cares.

    A terminal needs nothing: `assistant_block(final=True)` already colours
    that block. The desktop window cannot see colour (its pane strips ANSI),
    so it hands the harness a stream with an `answer_start()` method and tags
    the lines that follow instead. Streams without the method - a real
    console, a test capture, a file - are left alone.
    """
    stream = sys.stdout if stream is None else stream
    hook = getattr(stream, "answer_start", None)
    if callable(hook):
        hook()


def term_width(*, minimum: int = 44, maximum: int | None = None) -> int:
    """How many columns a line of output may use.

    OUTPUT_WIDTH is the ceiling, so the transcript keeps the same shape on
    every screen; the terminal's own width can only lower it, never raise it.
    Folding wider than the window would help nobody - the terminal would
    hard-wrap the row and the block would lose its left rail. `maximum` lets a
    caller ask for a tighter budget still. A deliberately tiny OUTPUT_WIDTH
    also lowers the floor, so a very narrow setting is not silently overruled.
    """
    limit = OUTPUT_WIDTH if maximum is None else min(maximum, OUTPUT_WIDTH)
    try:
        columns = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        columns = 80
    return max(min(minimum, limit), min(columns, limit))


def fit(text: str, limit: int, stream: Any = None) -> str:
    """Truncate to `limit` characters, marking the cut with an ellipsis."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    mark = glyphs(stream)["ellipsis"]
    return text[: max(0, limit - len(mark))] + mark


def panel(title: str, rows: list[tuple[str, str]], stream: Any = None) -> str:
    """A rounded box with aligned label/value columns."""
    g = glyphs(stream)
    width = term_width()
    label_w = max((len(label) for label, _ in rows), default=0)
    values = [fit(value, max(10, width - label_w - 8), stream) for _, value in rows]
    value_w = max((len(value) for value in values), default=0)
    inner = min(max(label_w + 2 + value_w, len(title) + 3, 24), width - 4)
    title = fit(title, max(1, inner - 2), stream)

    lines = [
        g["tl"] + g["h"] + " "
        + paint(title, "bold", "cyan", stream=stream) + " "
        + g["h"] * max(0, inner - len(title) - 1) + g["tr"]
    ]
    for (label, _), value in zip(rows, values):
        body = (
            paint(label.ljust(label_w), "dim", stream=stream)
            + "  " + value
            + " " * max(0, inner - label_w - 2 - len(value))
        )
        lines.append(g["v"] + " " + body + " " + g["v"])
    lines.append(g["bl"] + g["h"] * (inner + 2) + g["br"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Startup logo
# --------------------------------------------------------------------------- #
#
# Text art, not an image: a block-letter "U" carrying a harness, with the
# wordmark and a one-line description beside it. Two variants live here — one
# drawn with box/block characters, one pure ASCII — and banner() picks whichever
# the output encoding can actually render. Each row is (art, text, text role);
# keeping the text column separate means the layout never drifts.

_LOGO_ROWS: dict[str, list[tuple[str, str, str]]] = {
    "block": [
        ("   ██╗    ██╗", "", ""),
        ("   ██║    ██║", "U · H A R N E S S", "wordmark"),
        ("   ██║    ██║", "─" * 21, "rule"),
        ("   ╚██╗  ██╔╝", "reads · writes · runs", "text"),
        ("    ╚█████╔╝", "asks before it edits", "text"),
        ("    ╚══════╝", "deepseek tool-calling loop", "text"),
    ],
    "ascii": [
        ("   _    _", "", ""),
        ("  | |  | |", "U - H A R N E S S", "wordmark"),
        ("  | |  | |", "-" * 21, "rule"),
        ("  | |__| |", "reads . writes . runs", "text"),
        ("   \\____/", "asks before it edits", "text"),
        ("", "deepseek tool-calling loop", "text"),
    ],
    # --tester: same art, the other job.
    "block-tester": [
        ("   ██╗    ██╗", "", ""),
        ("   ██║    ██║", "U · T E S T E R", "wordmark"),
        ("   ██║    ██║", "─" * 21, "rule"),
        ("   ╚██╗  ██╔╝", "reads · runs · reports", "text"),
        ("    ╚█████╔╝", "never edits the code", "text"),
        ("    ╚══════╝", "finds bugs, writes Testing.md", "text"),
    ],
    "ascii-tester": [
        ("   _    _", "", ""),
        ("  | |  | |", "U - T E S T E R", "wordmark"),
        ("  | |  | |", "-" * 21, "rule"),
        ("  | |__| |", "reads . runs . reports", "text"),
        ("   \\____/", "never edits the code", "text"),
        ("", "finds bugs, writes Testing.md", "text"),
    ],
}

_LOGO_ROLE_STYLES: dict[str, tuple[str, ...]] = {
    "art": ("bold", "cyan"),
    "wordmark": ("bold", "bright_cyan"),
    "rule": ("grey",),
    "text": ("dim",),
}
_LOGO_PROBE = "█╗║╚╔╝·─"


def banner(stream: Any = None, *, tester: bool = False) -> str:
    """The startup logo, as text art the terminal can display.

    Two wordmarks (U-HARNESS / U-TESTER) in two encodings (block / ASCII), so
    the logo always says which job this run is doing.
    """
    key = "block" if encodable(_LOGO_PROBE, stream) else "ascii"
    rows = _LOGO_ROWS[key + "-tester" if tester else key]
    art_width = max(len(art) for art, _, _ in rows)
    out: list[str] = []
    for art, text, role in rows:
        line = paint(art.ljust(art_width), *_LOGO_ROLE_STYLES["art"], stream=stream)
        if text:  # last column: no padding, so lines never end in spaces
            line += "  " + paint(text, *_LOGO_ROLE_STYLES.get(role, ()), stream=stream)
        out.append(line.rstrip())
    return "\n".join(out)


def notice(kind: str, text: str, stream: Any = None, *, force: bool = False,
           style: str | None = None, background: str | None = None) -> str:
    """One aligned status line: "  ▲ warn  message".

    Plumbing notices (run/context/usage/skill) are suppressed in quiet mode.
    Warnings and errors always print: hiding a denied command or a retry would
    leave you staring at a stopped agent with no reason given. Pass force=True
    when the user typed a command and is waiting for its answer.

    `style` colours the message itself (not just the glyph and label). The
    colour is applied to each folded row, after folding, so a long value - a
    whole shell command, say - can be painted and wrapped at the same time.

    `background` turns the notice into a filled band instead: every row of it,
    rail and label included, sits on that background and is padded out to the
    transcript width, so the line reads as a bar across the screen rather than
    a stripe that stops mid-sentence. Text on a band is drawn in black, which
    is what keeps a light-to-mid background legible; with colour off there is
    no band to draw, so nothing is padded and the rows print plain.
    `background` wins over `style`.
    """
    if not force and not SHOW_NOTICES and kind in {"run", "context", "usage", "skill", "info"}:
        return ""
    glyph_name, colour, label = _NOTICE.get(kind, _NOTICE["info"])
    g = glyphs(stream)
    head = f"  {g[glyph_name]} "
    if label:
        head += label + "  "
    line = paint(f"  {g[glyph_name]} ", colour, stream=stream)
    if label:
        line += paint(label, "bold", colour, stream=stream) + "  "
    dim = kind in {"usage", "context", "info"}
    # A long message folds under itself, lined up with the text it continues,
    # instead of running off the right-hand edge of the screen. The fold runs
    # on the plain text: a colour code must never be split in half.
    pad = " " * display_width(head)
    rows = wrap(text, max(20, term_width() - display_width(head)))
    if background:
        # Padding is only worth writing when a band will actually be drawn;
        # piped output and NO_COLOR get the plain rows they always got.
        fill = term_width() if supports_color(stream) else 0
        band: list[str] = []
        for index, row in enumerate(rows):
            plain = (head if index == 0 else pad) + row
            plain += " " * max(0, fill - display_width(plain))
            tone = (("bold", "black", background) if index == 0
                    else ("black", background))
            band.append(paint(plain, *tone, stream=stream))
        return "\n".join(band)
    if dim:
        rows = [paint(row, "dim", stream=stream) for row in rows]
    elif style:
        rows = [paint(row, style, stream=stream) for row in rows]
    message = rows[0] + "".join(f"\n{pad}{row}" for row in rows[1:])
    return line + message


def approval_prompt(action: str, detail: str, stream: Any = None) -> str:
    """The "? Run command  <command>" line, with the detail folded under it.

    `detail` is usually a shell command, and a command you cannot read is a
    command you cannot approve - so it wraps at the transcript width instead of
    running off the edge, with the continuation rows lined up under it.
    """
    head = (
        "  " + paint("?", "bold", "yellow", stream=stream)
        + " " + paint(action, "bold", stream=stream)
    )
    # The fold budget excludes the continuation indent, so a wrapped row plus
    # its padding still fits the transcript width.
    pad = " " * (display_width("  ? ") + display_width(action) + 2)
    rows = wrap(detail, max(20, term_width() - display_width(pad)))
    if not rows[0]:
        return head
    return (
        head + paint("  " + rows[0], "dim", stream=stream)
        + "".join(f"\n{pad}{paint(row, 'dim', stream=stream)}" for row in rows[1:])
    )


def tier_badge(harness: Harness, stream: Any = None) -> str:
    """The "free"/"paid" line that sits above the prompt.

    One dim line so the tier in use is always visible: "free  deepseek-v4.1-
    flash:free" or "paid  deepseek-flash". The colour is the point - green for
    the free tier, yellow for paid - so a glance is enough. The context size
    rides on the same line, so how much of the window the conversation has
    used is visible without typing /context:

        ● free  deepseek-v4.1-flash:free  • context 3.4k/100k tokens (3%)
    """
    if harness.free:
        label, colour = "free", "green"
    else:
        label, colour = "paid", "yellow"
    g = glyphs(stream)
    head = (
        "  " + paint(g["bullet"], colour, stream=stream)
        + " " + paint(label, "bold", colour, stream=stream)
        + " " + paint(harness.model, "dim", stream=stream)
    )
    plain_head = f"  {g['bullet']} {label} {harness.model}"

    # Narrow terminals get a shorter tail before the badge is allowed to wrap:
    # the word "tokens" goes, then the word "context", then the figure itself.
    summary = context_summary(harness.messages)
    short = summary.replace(" tokens", "")
    tails = [f"context {summary}"]
    for tail in (f"context {short}", short):
        if tail not in tails:
            tails.append(tail)
    for tail in tails:
        if display_width(f"{plain_head}  {g['bullet']} {tail}") <= term_width():
            return (head + "  " + paint(f"{g['bullet']} {tail}", "dim",
                                       stream=stream))
    return head


def _split_at(text: str, budget: int) -> tuple[str, str]:
    """Peel off the longest head of `text` that fits in `budget` columns.

    Prefers the last space inside the head, so words stay whole; falls back to
    a hard cut only when the budget falls inside a single very long token.
    Wide (CJK) characters count double and combining marks ride along with
    the character they belong to, matching display_width().
    """
    width = 0
    cut = 0
    for index, char in enumerate(text):
        if unicodedata.combining(char):
            continue                     # no width of its own
        step = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if width + step > budget:
            break
        width += step
        cut = index + 1
    else:
        return text, ""                  # the whole thing fits
    if cut == 0:                         # one character already too wide
        return text[:1], text[1:]
    head = text[:cut]
    space = head.rfind(" ")
    if space > 0:                        # break on the word boundary
        word_break = head[:space].rstrip(" ")
        if word_break:                   # (a head of pure spaces keeps its cut)
            return word_break, text[space + 1:]
    return head, text[cut:]


def _fold_line(line: str, limit: int) -> list[str]:
    """Split one line into pieces that each fit in `limit` terminal columns.

    Continuation pieces repeat the line's own indentation, so a nested list
    keeps its shape once folded. A line carrying an escape sequence is left
    alone: splitting it could cut a colour code in half.
    """
    if limit <= 0 or "\x1b" in line or display_width(line) <= limit:
        return [line]
    indent = line[: len(line) - len(line.lstrip(" "))]
    if display_width(indent) * 2 >= limit:   # a deeper indent would leave no room
        indent = ""
    pieces: list[str] = []
    lead = ""                                # the first row keeps the line's own
    rest = line
    while rest:
        budget = limit - display_width(lead)
        if display_width(rest) <= budget:
            pieces.append(lead + rest)
            break
        head, tail = _split_at(rest, budget)
        if not head:
            pieces.append(lead + rest)
            break
        pieces.append(lead + head)
        rest, lead = tail, indent
    return pieces or [""]


def wrap(text: str, limit: int) -> list[str]:
    """Fold every line of `text` so each returned row fits `limit` columns.

    Multi-line text keeps its line breaks; each line is folded on its own with
    _fold_line(), which also carries the line's indentation onto continuation
    rows. Folding happens on plain text, before any colour is applied - a line
    already carrying an escape sequence is left whole by _fold_line().
    """
    rows: list[str] = []
    for line in text.expandtabs(8).splitlines() or [""]:
        rows.extend(_fold_line(line, limit))
    return rows or [""]


def assistant_block(text: str, stream: Any = None, *, final: bool = False) -> str:
    """The model's prose in a coloured left-bordered block.

    Long lines are folded here instead of being left to the terminal. The
    terminal wraps at the right margin without knowing about the "│ " rail,
    so every wrapped row but the first lost its left edge; folding beforehand
    means each row of the block carries the bar and the whole block keeps its
    outline at any width.

    `final=True` marks the block that answers the request rather than the
    running commentary around a tool call: heading and prose are drawn in
    ANSWER_COLOUR so the answer is the one thing that looks different.
    """
    g = glyphs(stream)
    bar = paint(g["v"], "grey", stream=stream)
    colour = ANSWER_COLOUR if final else "green"
    head = (
        paint(f"  {g['dot']} ", colour, stream=stream)
        + paint("assistant", "bold", colour, stream=stream)
    )
    lines = text.strip().splitlines() or [""]
    # One column for the bar, one for the space after it, plus the indent.
    gutter = display_width(RESULT_INDENT) + display_width(g["v"]) + 1
    limit = max(16, term_width() - gutter - 1)      # -1: never end on the margin
    body: list[str] = []
    for line in lines:
        # Tabs move to the terminal's own 8-column stops before measuring, so
        # the fold lands where the screen actually wraps. Nothing else is
        # rewritten: the line still reads as the model wrote it.
        for piece in _fold_line(line.expandtabs(8), limit):
            piece = piece.rstrip()
            if not piece:
                body.append(f"{RESULT_INDENT}{bar}")
            elif final:
                # Only the words change colour: the rail stays grey so the
                # block keeps its outline.
                body.append(
                    f"{RESULT_INDENT}{bar} "
                    + paint(piece, colour, stream=stream)
                )
            else:
                body.append(f"{RESULT_INDENT}{bar} {piece}")
    return "\n".join([head, *body])


def describe_value(value: Any) -> str:
    """A short, human-sized description of one tool argument.

    A one-line string (a shell command, a search pattern, a path) is quoted as
    it stands, up to ARG_VALUE_CHARS: tool_call_block() folds it onto further
    rows rather than cutting it off, so what the model is about to run is
    readable in full. Multi-line values stay summarised by size - dumping a
    file's contents as escaped JSON would bury the rest of the call.
    """
    if isinstance(value, str):
        if "\n" in value.strip("\n"):
            lines = value.splitlines() or [""]
            preview = next((line.strip() for line in lines if line.strip()), "")
            suffix = f"  {short(preview, 44)}" if preview else ""
            return f"{len(lines)} lines, {len(value)} chars{suffix}"
        return short(json.dumps(value, ensure_ascii=False), ARG_VALUE_CHARS)
    if isinstance(value, (bool, int, float)) or value is None:
        return json.dumps(value)
    if isinstance(value, list):
        return f"{len(value)} items"
    if isinstance(value, dict):
        return f"{len(value)} keys"
    return short(value, ARG_VALUE_CHARS)


def describe_args(raw_args: str) -> list[tuple[str, str]]:
    try:
        args = json.loads(raw_args or "{}")
    except (json.JSONDecodeError, ValueError):
        return [("arguments", short(raw_args, 100))]
    if not isinstance(args, dict) or not args:
        return []
    return [(str(key), describe_value(value)) for key, value in args.items()]


def tool_call_block(name: str, raw_args: str, stream: Any = None) -> str:
    """`⚙ write_file` followed by one aligned row per argument.

    A long argument - typically the shell command of execute_command - is
    folded onto further rows under its own label instead of being cut off with
    an ellipsis, so what the agent is about to run stays readable at any
    width. ARG_MAX_ROWS bounds how much room one argument may take; a value
    cut there ends in an ellipsis, marking that more was left out.

    Multi-line arguments such as file contents are summarised by size instead
    of being dumped as one enormous escaped JSON string.
    """
    g = glyphs(stream)
    head = (
        paint(f"  {g['gear']} ", "cyan", stream=stream)
        + paint(name, "bold", "cyan", stream=stream)
    )
    rows = describe_args(raw_args)
    if not rows:
        return head + paint("  (no arguments)", "dim", stream=stream)
    label_w = max(len(label) for label, _ in rows)
    limit = max(16, term_width() - label_w - 10)
    out = [head]
    for label, value in rows:
        pieces = wrap(value, limit)
        if len(pieces) > ARG_MAX_ROWS:
            # Keep the block's shape: the cut is marked, the tail is dropped.
            # _split_at() peels off a head that fits, so the row keeps its
            # width budget and only one ellipsis ends up on the line.
            pieces = pieces[:ARG_MAX_ROWS]
            mark = g["ellipsis"]
            room = limit - display_width(mark) - 1
            pieces[-1] = _split_at(pieces[-1], room)[0].rstrip() + " " + mark
        for index, piece in enumerate(pieces):
            # Continuation rows line up under the value they continue, so the
            # label column stays a column.
            lead = (
                paint(label.ljust(label_w), "dim", stream=stream)
                if index == 0 else " " * label_w
            )
            out.append("      " + lead + "  " + piece)
    return "\n".join(out)


def _diff_styles(line: str) -> tuple[str, ...]:
    """The colour a diff line gets, by its first character."""
    if line.startswith(("+++", "---")):
        return ("bold",)
    if line.startswith("@@"):
        return ("cyan",)
    if line.startswith("+"):
        return ("green",)
    if line.startswith("-"):
        return ("red",)
    return ("dim",)


def diff_line(line: str, stream: Any = None) -> str:
    return paint(line, *_diff_styles(line), stream=stream)


def result_block(result: str, stream: Any = None, *, summary_only: bool | None = None) -> str:
    """Render a tool result: short ones inline, long ones as a capped block.

    In summary-only mode (the default under --brief/--quiet) only the headline
    is drawn - "⚙ read_file" above, then "↳ 71 lines" below. The result itself
    still goes to the model and to --log; it just is not echoed at you.
    """
    hide_body = BRIEF_RESULTS if summary_only is None else summary_only
    g = glyphs(stream)
    text = result.rstrip("\r\n")
    if not text.strip():
        return paint(f"  {g['result']} (no output)", "dim", stream=stream)

    lines = text.splitlines()
    exit_code: int | None = None
    match = re.fullmatch(r"\s*exit_code:\s*(-?\d+)\s*", lines[0])
    if match:
        exit_code = int(match.group(1))
        lines = lines[1:]

    failed = (exit_code is not None and exit_code != 0) or text.lstrip().lower().startswith(
        ("error", "blocked", "invalid json")
    )
    is_diff = any(line.startswith("@@") for line in lines) or (
        bool(lines) and lines[0].startswith("--- ")
    )
    # Room left on a headline row for a quoted reason: "  ↳ ✖ exit 1  ...".
    room = max(20, term_width() - 16)

    # A one-line answer reads best on one line: "↳ Created user.py (120 bytes)".
    # Kept in brief mode as well: it is the outcome of the call, not its body.
    if (
        exit_code is None
        and not is_diff
        and len(lines) == 1
        and len(lines[0]) <= term_width() - len(RESULT_INDENT) - 6
    ):
        mark = paint(f"{g['fail']} ", "bold", "red", stream=stream) if failed else ""
        line = paint(lines[0], "red", stream=stream) if failed else lines[0]
        return f"  {paint(g['result'], 'grey', stream=stream)} {mark}{line}"

    if hide_body:
        # One line per call: what happened and how much came back, no body.
        body = [
            line for line in lines
            if line.strip() not in {"stdout:", "stderr:", "(empty)"}
        ]
        if exit_code is not None:
            head = paint(f"  {g['result']} ", "grey", stream=stream)
            if exit_code == 0:
                head += paint(f"{g['ok']} exit 0", "bold", "green", stream=stream)
                if body:
                    head += paint(f" - {len(body)} line(s) of output hidden",
                                  "dim", stream=stream)
                else:
                    head += paint(" - no output", "dim", stream=stream)
            else:
                # A failing command still shows why: hide the dump, not the reason.
                head += paint(f"{g['fail']} exit {exit_code}", "bold", "red",
                              stream=stream)
                reason = next((line.strip() for line in body if line.strip()), "")
                if reason:
                    head += paint(f"  {short(reason, room)}", "red", stream=stream)
                elif body:
                    head += paint(f" - {len(body)} line(s) of output hidden",
                                  "dim", stream=stream)
        elif failed:
            reason = next((line.strip() for line in lines if line.strip()), "")
            head = paint(f"  {g['result']} ", "grey", stream=stream)
            head += paint(f"{g['fail']} error", "bold", "red", stream=stream)
            if reason:
                head += paint(f"  {short(reason, room)}", "red", stream=stream)
        elif is_diff:
            added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
            removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
            head = paint(f"  {g['result']} ", "grey", stream=stream)
            head += paint(f"diff +{added} -{removed}", "dim", stream=stream)
        else:
            head = paint(f"  {g['result']} ", "grey", stream=stream)
            head += paint(f"{len(lines)} line(s) of output hidden", "dim",
                          stream=stream)
        return head

    head = paint(f"  {g['result']} ", "grey", stream=stream)
    body = [line for line in lines if line.strip() not in {"stdout:", "stderr:"}]
    if exit_code is not None and SHOW_EXIT_BADGE:
        badge = f"{g['ok']} exit 0" if exit_code == 0 else f"{g['fail']} exit {exit_code}"
        head += paint(badge, "bold", "green" if exit_code == 0 else "red", stream=stream)
        if body:
            head += paint(f"  {len(body)} lines", "dim", stream=stream)
        else:
            head += paint("  no output", "dim", stream=stream)
    elif failed:
        head += paint(f"{g['fail']} error", "bold", "red", stream=stream)

    bar = paint(g["v"], "grey", stream=stream)
    limit = max(20, term_width() - len(RESULT_INDENT) - 2)
    shown, hidden = lines[:MAX_RESULT_LINES], len(lines) - MAX_RESULT_LINES
    out = [head]
    in_stderr = False
    for line in shown:
        stripped = line.strip()
        if stripped in {"stdout:", "stderr:"}:
            in_stderr = stripped == "stderr:"
            label = paint(stripped[:-1], "italic", "grey", stream=stream)
            out.append(RESULT_INDENT + bar + " " + label)
            continue
        # A long result line wraps instead of being cut with "…": the text of
        # a failing command is worth reading in full, and every row carries the
        # left rail, so the block keeps its shape. The colour is chosen from
        # the whole line and used for each of its rows, so a folded "+" diff
        # line stays green.
        if is_diff:
            styles: tuple[str, ...] = _diff_styles(line)
        elif in_stderr:
            styles = ("yellow",)
        else:
            styles = ()
        for piece in _fold_line(line, limit):
            rendered = paint(piece, *styles, stream=stream) if styles else piece
            out.append((RESULT_INDENT + bar + " " + rendered).rstrip())
    if hidden > 0:
        out.append(
            RESULT_INDENT + bar + " "
            + paint(f"{g['ellipsis']} +{hidden} more lines", "dim", stream=stream)
        )
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Reading a line
# --------------------------------------------------------------------------- #
#
# input() leans on the terminal's line discipline and on readline. When either
# one is missing - piped stdin, a bare pty, a web terminal, a Windows console -
# pressing backspace no longer edits the line: the raw key (^?, ^H) or the raw
# escape sequence (^[[D, ^[[A) is simply appended to the text and ends up in
# the prompt sent to the model. read_line() below reads the keys itself while a
# real terminal is attached, and interprets (or discards) editing keys in every
# other case, so the text handed back never contains stray control characters.

_ANSI_SEQUENCE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

# Escape sequences produced by the cursor/editing keys we care about.
_ESCAPE_KEYS = {
    "A": "UP",
    "B": "DOWN",
    "C": "RIGHT",
    "D": "LEFT",
    "H": "HOME",
    "F": "END",
    "1~": "HOME",
    "7~": "HOME",
    "4~": "END",
    "8~": "END",
    "3~": "DELETE",
}

# Set HARNESS_INPUT=builtin to go back to plain input() everywhere.
_FORCE_BUILTIN_INPUT = {"builtin", "input", "readline"}


def strip_ansi(text: str) -> str:
    """`text` without ANSI colour codes."""
    return _ANSI_SEQUENCE.sub("", text)


def display_width(text: str) -> int:
    """Columns `text` occupies on a terminal (wide characters count double)."""
    width = 0
    for char in strip_ansi(text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def sanitise_line(text: str) -> str:
    """Remove control characters a terminal may have leaked into a line."""
    return "".join(char for char in text if char == "\t" or char.isprintable())


def apply_editing_keys(raw: str) -> str:
    """Interpret backspaces in `raw` and drop escape sequences.

    Used when the input did not come from an interactive terminal (piped or
    pasted text), where the editing keys were never processed for us.
    """
    out: list[str] = []
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if char in "\x7f\x08":                      # backspace / delete
            if out:
                out.pop()
        elif char == "\x1b":                        # start of an escape sequence
            index += 1
            if index < length and raw[index] in "[O":
                index += 1
                while index < length and not ("@" <= raw[index] <= "~"):
                    index += 1
            index += 1                              # skip the final byte too
            continue
        elif char == "\t" or char.isprintable():
            out.append(char)
        index += 1
    return "".join(out)


class _PosixKeyReader:
    """Single key presses from a POSIX terminal running in raw mode."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._chars: list[str] = []

    def _more(self, timeout: float | None) -> bool:
        """Buffer at least one character, or report end of input."""
        if self._chars:
            return True
        if timeout is not None and not select.select([self.fd], [], [], timeout)[0]:
            return False
        while True:
            chunk = os.read(self.fd, 1)
            if not chunk:
                return False
            text = self._decoder.decode(chunk)
            if text:
                self._chars.extend(text)
                return True

    def _escape(self) -> str:
        """Decode the rest of an escape sequence into a key name."""
        if not self._more(0.05):
            return ""                               # a lone Esc: ignore it
        lead = self._chars.pop(0)
        if lead not in "[O":
            return ""
        sequence = ""
        while len(sequence) <= 8:
            if not self._more(0.05):
                return ""
            char = self._chars.pop(0)
            sequence += char
            if "@" <= char <= "~":
                return _ESCAPE_KEYS.get(sequence, "")
        return ""

    def next(self) -> str:
        if not self._more(None):
            return "EOF"
        char = self._chars.pop(0)
        if char in "\r\n":
            return "ENTER"
        if char in "\x7f\x08":
            return "BACKSPACE"
        if char == "\x03":
            return "INTERRUPT"
        if char == "\x04":
            return "END_OF_FILE"
        if char == "\x01":
            return "HOME"
        if char == "\x05":
            return "END"
        if char == "\x0b":
            return "KILL_TAIL"
        if char == "\x15":
            return "KILL_LINE"
        if char == "\x17":
            return "KILL_WORD"
        if char == "\x1b":
            return self._escape()
        if char == "\t" or not char.isprintable():
            return ""
        return char


class _WindowsKeyReader:
    """Single key presses from a Windows console."""

    _SPECIAL: ClassVar[dict[str, str]] = {
        "H": "UP",
        "P": "DOWN",
        "K": "LEFT",
        "M": "RIGHT",
        "G": "HOME",
        "O": "END",
        "S": "DELETE",
    }

    def next(self) -> str:
        import msvcrt

        char = msvcrt.getwch()
        if char in "\r\n":
            return "ENTER"
        if char == "\x08":
            return "BACKSPACE"
        if char == "\x03":
            return "INTERRUPT"
        if char == "\x04":
            return "END_OF_FILE"
        if char == "\x1b":
            return self._escape()
        if char in ("\x00", "\xe0"):                # prefix of a special key
            return self._SPECIAL.get(msvcrt.getwch(), "")
        if char == "\t" or not char.isprintable():
            return ""
        return char

    def _escape(self) -> str:
        import msvcrt

        if not msvcrt.kbhit():
            return ""
        lead = msvcrt.getwch()
        if lead not in "[O":
            return ""
        sequence = ""
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            sequence += char
            if "@" <= char <= "~":
                break
        return _ESCAPE_KEYS.get(sequence, "")


def _input_columns() -> int:
    """Columns available to the line being edited, re-read on every redraw.

    get_terminal_size() honours $COLUMNS first, so a resized terminal (or a
    test harness) can steer the wrapping without an ioctl.
    """
    try:
        return max(1, shutil.get_terminal_size((80, 24)).columns)
    except (OSError, ValueError):
        return 80


def _run_editor(prompt: str, history: list[str], keys: Any) -> str:
    """Shared line editor: redraws the line as the user edits it.

    A line longer than the terminal is wide wraps onto more rows, so the
    editor tracks which row the caret is on. Every redraw walks back up to
    the first row of the input, erases everything below it and prints the
    line again. Erasing to the end of the screen (not just the end of the
    current row) is what keeps the wrapped rows of a long line from being
    left behind as duplicates.
    """
    out = sys.stdout
    text: list[str] = []
    cursor = 0
    hist_index = len(history)
    draft: list[str] = []

    # A prompt may start with newlines ("\nyou> "); print those once, then
    # keep redrawing only the visible part.
    cut = prompt.rfind("\n")
    head, inline = (prompt[: cut + 1], prompt[cut + 1:]) if cut >= 0 else ("", prompt)
    prompt_width = display_width(inline)          # colour codes do not count

    columns = _input_columns()
    row = col = 0                                 # caret, counted from row 0

    def started() -> str:
        """Escape codes that put the caret in column 0 of the first row."""
        return f"\x1b[{row}A\r" if row else "\r"

    def move_to(target_row: int, target_col: int) -> None:
        """Walk the caret from where it is now to (target_row, target_col)."""
        nonlocal row, col
        if target_row < row:
            out.write(f"\x1b[{row - target_row}A")
        elif target_row > row:
            out.write(f"\x1b[{target_row - row}B")
        if target_col < col:
            out.write(f"\x1b[{col - target_col}D")
        elif target_col > col:
            out.write(f"\x1b[{target_col - col}C")
        row, col = target_row, target_col

    def drawn() -> int:
        """Cells the prompt plus the text occupy on screen."""
        return prompt_width + display_width("".join(text))

    def drawn_prefix(index: int) -> int:
        """Cells used by the prompt plus `text[:index]`."""
        return prompt_width + display_width("".join(text[:index]))

    def draw() -> None:
        nonlocal columns, row, col
        width = _input_columns()
        if width != columns:
            # A resize re-wrapped the screen under us: work out from the text
            # that was last drawn which row the caret ended up on.
            columns = width
            row = drawn_prefix(cursor) // columns
            col = 0
        joined = "".join(text)
        out.write(started())                      # column 0 of the first row
        out.write("\x1b[J")                       # wipe the line and every
        out.write(inline + joined)                # wrapped row below it
        # Print one space, then step back over it. Without it a character in
        # the last column leaves the caret in the terminal's pending-wrap
        # state, where "move left" and "write a character" disagree about
        # which cell the caret is really in.
        total = prompt_width + display_width(joined)
        out.write(" \x1b[D")
        # The space advances one cell; if that lands on the margin the caret
        # wraps to the row below. "\x1b[D" then steps back one column, but a
        # terminal clamps it at column 0 rather than crossing to the previous
        # row - so track where the caret really is, not where total suggests.
        after_row, after_col = divmod(total + 1, columns)
        if after_col == 0:
            row, col = after_row, 0
        else:
            row, col = divmod(total, columns)
        caret_row, caret_col = divmod(drawn_prefix(cursor), columns)
        move_to(caret_row, caret_col)
        out.flush()

    def finish() -> None:
        """Drop the padding space and leave the caret below the line."""
        total = drawn()
        move_to(*divmod(total, columns))
        out.write("\x1b[K")                       # erase only the padding
        if total % columns or not total:
            out.write("\n")                       # caret is mid-row
        out.flush()                               # (else the wrap already did)

    if head:
        out.write(head)
    draw()

    while True:
        key = keys.next()

        if key == "ENTER":
            finish()
            return "".join(text)
        if key == "INTERRUPT":
            finish()
            raise KeyboardInterrupt
        if key == "EOF" or (key == "END_OF_FILE" and not text):
            finish()
            raise EOFError
        if key == "BACKSPACE":
            if cursor:
                del text[cursor - 1]
                cursor -= 1
                draw()
        elif key == "DELETE":
            if cursor < len(text):
                del text[cursor]
                draw()
        elif key == "LEFT":
            if cursor:
                cursor -= 1
                draw()
        elif key == "RIGHT":
            if cursor < len(text):
                cursor += 1
                draw()
        elif key == "HOME":
            if cursor:
                cursor = 0
                draw()
        elif key == "END":
            if cursor != len(text):
                cursor = len(text)
                draw()
        elif key == "KILL_LINE":
            if cursor:
                del text[:cursor]
                cursor = 0
                draw()
        elif key == "KILL_TAIL":
            if cursor < len(text):
                del text[cursor:]
                draw()
        elif key == "KILL_WORD":
            start = cursor
            while start and text[start - 1] == " ":
                start -= 1
            while start and text[start - 1] != " ":
                start -= 1
            if start != cursor:
                del text[start:cursor]
                cursor = start
                draw()
        elif key == "UP":
            if hist_index > 0:
                if hist_index == len(history):
                    draft = list(text)
                hist_index -= 1
                text = list(history[hist_index])
                cursor = len(text)
                draw()
        elif key == "DOWN":
            if hist_index < len(history):
                hist_index += 1
                text = list(draft) if hist_index == len(history) else list(history[hist_index])
                cursor = len(text)
                draw()
        elif len(key) == 1:
            text.insert(cursor, key)
            cursor += 1
            draw()


def _edit_line_posix(prompt: str, history: list[str]) -> str:
    """Run the line editor with the terminal switched to raw mode."""
    import termios

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    raw = termios.tcgetattr(fd)
    raw[0] &= ~(termios.IXON | termios.ICRNL)       # keep ^S/^Q out of the way
    raw[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
    raw[6][termios.VMIN] = 1
    raw[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, raw)
    try:
        return _run_editor(prompt, history, _PosixKeyReader(fd))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def read_line(prompt: str = "", *, history: list[str] | None = None) -> str:
    """Read one line, honouring backspace and the arrow keys.

    The returned text is always free of control characters, so editing keys
    can never leak into the message that is sent to the model.
    """
    if os.environ.get("HARNESS_INPUT", "").strip().lower() in _FORCE_BUILTIN_INPUT:
        return sanitise_line(input(prompt))

    if not (_is_tty(sys.stdin) and _is_tty(sys.stdout)):
        # No terminal to redraw on: let input() read, then undo any backspaces
        # and drop escape sequences it could not interpret.
        return apply_editing_keys(input(prompt))

    try:
        if os.name == "nt":
            return sanitise_line(_run_editor(prompt, history or [], _WindowsKeyReader()))
        return sanitise_line(_edit_line_posix(prompt, history or []))
    except (OSError, ValueError, ImportError):
        # Unusual terminal: fall back to whatever Python's input() does.
        return sanitise_line(input(prompt))


def session_preview(messages: list[dict[str, Any]], limit: int = SESSION_PREVIEW_CHARS) -> str:
    """First line of user text in a saved conversation, for the picker."""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = " ".join(str(message.get("content") or "").split())
        if not text or text.startswith(SKILL_BLOCK_PREFIX):
            continue  # loaded skill text is not what the user asked for
        return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"
    return "(empty conversation)"


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #
#
# A skill is a markdown file of instructions the user can pull into the
# conversation by typing @{skill-name}. Two layouts are supported:
#
#     skills/<name>/SKILL.md     a folder, free to carry extra reference files
#     skills/<name>.md           a single file
#
# "skills/" here means either of the two folders searched by default: the
# workspace's own skills/ and the skills/ folder next to this script (see
# SkillBook). Both are searched every time, the workspace first.
#
# An optional "---" frontmatter header supplies name and description; without
# one the file (or folder) name and the first line do the job. Nothing is read
# from a skill until the user references it, so a large skills/ folder costs
# only one line per skill in the system prompt.

SKILL_BLOCK_PREFIX = "[skills]"


@dataclass(frozen=True)
class Skill:
    """One discovered skill, already read from disk."""

    name: str
    description: str
    path: Path
    body: str
    origin: str = ""     # the name to match on when it differs from `name`


def referenced_skills(text: str) -> list[str]:
    """Every distinct @{skill} name in `text`, in the order they appear."""
    names: list[str] = []
    for match in SKILL_REF_RE.finditer(text or ""):
        name = match.group(1).strip()
        if name and name.lower() not in {seen.lower() for seen in names}:
            names.append(name)
    return names


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Separate an optional --- key: value --- header from the body."""
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta, text.strip()
    lines = text.splitlines()
    end = next(
        (index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}),
        None,
    )
    if end is None:                       # an unterminated header is just text
        return meta, text.strip()
    for raw in lines[1:end]:
        key, separator, value = raw.partition(":")
        if not separator:
            continue
        key = key.strip().lower()
        if key:
            meta[key] = value.strip().strip("\"'")
    return meta, "\n".join(lines[end + 1:]).strip()


def first_line(text: str, limit: int = 120) -> str:
    """The first meaningful line of a body, used as a fallback description."""
    for line in text.splitlines():
        stripped = line.strip().lstrip("#>-*+ ").strip()
        if stripped:
            return stripped if len(stripped) <= limit else stripped[: limit - 1] + "…"
    return ""


class SkillBook:
    """Discovers the skills under one or more directories, on demand.

    By default two folders are searched: the workspace's own ``skills/`` and
    the ``skills/`` folder next to ``uharness.py``. The workspace comes first,
    so a project skill wins over a bundled one with the same name, and the
    same packs are available whichever directory the harness is started from.

    A skill directory does not have to live inside the workspace. An absolute
    path (or ``~``) is accepted, which is how a skills folder kept in a dotfile
    directory stays usable from every project. Several roots can be given as a
    ``os.pathsep``-separated string; earlier roots win when two skills share a
    name. Passing `directory` explicitly (or setting ``HARNESS_SKILLS_DIR``)
    replaces the default pair completely.
    """

    def __init__(self, workspace: Path, directory: str | None = None) -> None:
        self.workspace = workspace
        if directory is not None:
            self.directory = str(directory)
            bundled = False
        else:
            from_env = os.environ.get(HARNESS_SKILLS_DIR_ENV, "").strip()
            self.directory = from_env or SKILLS_DIR_NAME
            bundled = not from_env
        names = [
            part.strip() for part in self.directory.split(os.pathsep) if part.strip()
        ] or [SKILLS_DIR_NAME]
        candidates: list[Path] = []
        for name in names:
            candidate = Path(name).expanduser()
            candidates.append(
                candidate if candidate.is_absolute() else workspace / candidate
            )
        if bundled:
            # No configuration at all: also look beside uharness.py, which is
            # where a checkout keeps the skills it ships with.
            candidates.append(SCRIPT_DIR / "skills")
        self.roots: list[Path] = []
        for candidate in candidates:
            root = candidate.resolve()
            if root not in self.roots:
                self.roots.append(root)

    @property
    def root(self) -> Path:
        """The first (highest-priority) root, for messages that need one."""
        return self.roots[0]

    def inside_workspace(self, path: Path) -> bool:
        """True when `path` sits inside the workspace the agent may edit."""
        try:
            path.relative_to(self.workspace)
        except ValueError:
            return False
        return True

    def external_roots(self) -> list[Path]:
        """Skill roots that live outside the workspace, so reads need granting."""
        return [root for root in self.roots if not self.inside_workspace(root)]

    def describe_roots(self) -> str:
        """Human-readable list of the places skills are looked up."""
        return " or ".join(f"{self.rel(root)}/" for root in self.roots)

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    def discover(self) -> list[Skill]:
        """Every readable skill, sorted by name. Cheap enough to call often."""
        found: dict[str, Skill] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            try:
                entries = sorted(root.iterdir(), key=lambda item: item.name.lower())
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    path = next(
                        (entry / name for name in SKILL_FILENAMES if (entry / name).is_file()),
                        None,
                    )
                    origin = entry.name
                elif entry.suffix.lower() == ".md":
                    path = entry
                    origin = entry.stem
                else:
                    continue
                if path is None:
                    continue
                skill = self._read(path, origin)
                if skill is not None:
                    found.setdefault(skill.name.lower(), skill)
        return sorted(found.values(), key=lambda skill: skill.name.lower())

    def _read(self, path: Path, origin: str) -> Skill | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if len(text) > MAX_SKILL_CHARS:
            text = clip(text, MAX_SKILL_CHARS, keep="head")
        meta, body = split_frontmatter(text)
        name = (meta.get("name") or "").strip() or origin
        description = (meta.get("description") or "").strip() or first_line(body)
        return Skill(
            name=name[:MAX_SKILL_NAME],
            description=description,
            path=path,
            body=body,
            origin=origin,
        )

    def find(self, name: str) -> Skill | None:
        """Look a skill up by name, case-insensitively, never by file path."""
        wanted = name.strip().lower()
        for skill in self.discover():
            if wanted in {skill.name.lower(), skill.origin.lower()}:
                return skill
        return None

    def bundled(self, skill: Skill) -> list[Path]:
        """Sibling files of a folder skill, so the model knows it may read them."""
        parent = skill.path.parent
        if parent in self.roots:
            return []
        try:
            return [
                item for item in sorted(parent.iterdir())
                if item.is_file() and item != skill.path and not item.name.startswith(".")
            ]
        except OSError:
            return []

    def index(self, limit: int = MAX_SKILL_INDEX) -> str:
        """One "- @{name} - description" line per skill, for the system prompt."""
        skills = self.discover()
        if not skills:
            return ""
        lines = [
            f"- @{{{skill.name}}}" + (f" - {skill.description}" if skill.description else "")
            for skill in skills
        ]
        if len(lines) > 1:
            kept: list[str] = []
            used = 0
            for line in lines:
                if used + len(line) + 1 > limit and kept:
                    break
                kept.append(line)
                used += len(line) + 1
            if len(kept) < len(lines):
                kept.append(f"- ... and {len(lines) - len(kept)} more (see /skills)")
            lines = kept
        return "\n".join(lines)


def skill_context(loaded: list[Skill], missing: list[str], book: SkillBook) -> str:
    """The message body that carries loaded skill text into the conversation."""
    parts = [
        f"{SKILL_BLOCK_PREFIX} The user loaded the skill(s) below with @{{name}}.",
        (
            "Treat their instructions as part of this request: apply them where "
            "they fit, keep them for the rest of the conversation, and say so if "
            "one conflicts with a higher-priority rule or with another loaded skill."
        ),
    ]
    for skill in loaded:
        header = f"\n### skill: {skill.name}\nsource: {book.rel(skill.path)}"
        if skill.description:
            header += f"\nsummary: {skill.description}"
        extras = book.bundled(skill)
        if extras:
            header += "\nbundled files: " + ", ".join(book.rel(path) for path in extras)
        if not book.inside_workspace(skill.path):
            # The skill sits outside the workspace, so the paths above are
            # absolute. Say what the model may do with them, and what it may not.
            header += (
                "\nlocation: this skill lives outside the workspace. Its files are "
                "readable with read_file, but they are not part of the workspace: do "
                "not edit or delete them, and copy anything you need into the "
                "workspace first."
            )
        parts.append(header)
        parts.append(skill.body or "(this skill file is empty)")
    if missing:
        available = ", ".join(f"@{{{skill.name}}}" for skill in book.discover()) or "none"
        parts.append(
            f"\n{SKILL_BLOCK_PREFIX} warning: no skill named "
            + ", ".join(f"@{{{name}}}" for name in missing)
            + f" exists in {book.describe_roots()}. Available skills: {available}. "
            "Carry on with the request without it."
        )
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Context size
# --------------------------------------------------------------------------- #
#
# What /context reports: how much conversation the next request has to carry.
# Tool-call arguments are part of that request, so they are counted too - a
# bulky JSON argument costs just as much as prose.

def message_chars(message: dict[str, Any]) -> int:
    """Characters one message adds to a request: its text plus tool arguments."""
    chars = len(str(message.get("content") or ""))
    for call in message.get("tool_calls") or ():
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict):
            chars += len(str(function.get("arguments") or ""))
    return chars


def estimate_tokens(chars: int, messages: int = 0) -> int:
    """Rough token count: ~4 characters each, plus a little per message."""
    if chars <= 0 and messages <= 0:
        return 0
    return (
        (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN + messages * TOKENS_PER_MESSAGE
    )


def context_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimated tokens the next request has to carry - the /context figure.

    Same arithmetic as /context, so the number on the model line and the one
    in the panel can never disagree.
    """
    return estimate_tokens(sum(message_chars(m) for m in messages), len(messages))


def context_budget_tokens() -> int:
    """The token budget compaction keeps the conversation under; 0 = no limit."""
    if MAX_HISTORY_CHARS <= 0:
        return 0
    return estimate_tokens(MAX_HISTORY_CHARS)


def format_tokens(tokens: int) -> str:
    """A count short enough for a status line: 940, 3.4k, 12.3k, 100k."""
    if tokens < 1_000:
        return f"{tokens:,}"
    thousands = tokens / 1_000
    if thousands < 100:
        return f"{thousands:.1f}k"
    return f"{thousands:.0f}k"


def context_summary(messages: list[dict[str, Any]]) -> str:
    """One-line context size: "3.4k/100k tokens (3%)", "empty", "12.3k tokens"."""
    tokens = context_tokens(messages)
    if tokens <= 0:
        return "empty"
    budget = context_budget_tokens()
    if budget <= 0:
        return f"{format_tokens(tokens)} tokens"
    share = 100 * tokens / budget
    percent = "<1%" if share < 1 else f"{share:.0f}%"
    return f"{format_tokens(tokens)}/{format_tokens(budget)} tokens ({percent})"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

class Harness:
    def __init__(
        self,
        workspace: Path,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        auto_approve: bool = False,
        allow_dangerous: bool = False,
        dry_run: bool = False,
        max_turns: int = 20,
        command_timeout: int = 120,
        thinking: bool = False,
        log_path: Path | None = None,
        show_usage: bool = False,
        skills_dir: str | None = None,
        autosave: bool = True,
        tester: bool | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.model = model
        self.auto_approve = auto_approve
        self.allow_dangerous = allow_dangerous
        self.dry_run = dry_run
        self.max_turns = max_turns
        self.command_timeout = command_timeout
        self.thinking = thinking
        self.log_path = log_path
        self.show_usage = show_usage
        # Console verbosity: "full", "brief" (no result bodies) or "quiet".
        self.verbosity = VERBOSITY
        self.sessions_dir = self.workspace / SESSION_DIR_NAME
        # Tester mode. When set, the write tools accept exactly one path - the
        # testing report - and delete_file is refused, so a tester run cannot
        # change the code it is reviewing. See resolve_write. None means "use
        # the module default", which main() sets from --tester; run_selftest()
        # passes an explicit value so the checks do not depend on the flag.
        self.tester = TESTER if tester is None else bool(tester)
        self.code_read_only = self.tester
        self.report_path = self.workspace / REPORT_PATH
        self.tools = tools_for(self.tester)
        # Autosave writes to last_session_name when this run already saved or
        # opened a conversation, so a resumed chat updates that one file
        # instead of piling up copies. It fires after every turn *and* on the
        # way out, so a crash or a killed terminal costs at most one turn
        # (--no-autosave turns the whole thing off).
        self.autosave = autosave
        self.last_session_name: str | None = None
        self._autosave_warned = False   # one warning per run, not one per turn
        # A skills directory may live outside the workspace; read_roots() turns
        # any such directory into a read-only grant (see resolve()).
        self.skills = SkillBook(self.workspace, skills_dir)
        self.loaded_skills: dict[str, str] = {}   # name -> body already in context
        self.usage: dict[str, int] = {}
        self.api_calls = 0
        self._compaction_noted = False
        # Undo: every turn opens one snapshot group, and the modifying tools
        # record what a path looked like before they touch it. The group is
        # created lazily, so a turn that changes nothing leaves no directory.
        self._undo_group: Path | None = None
        self._undo_warned = False
        # Which profile the client is currently built from ("free" or "paid").
        self.api_key = api_key
        self.free = self._infer_free()
        self.client = self._build_client()
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt()}
        ]

    # -- console verbosity -------------------------------------------------- #

    @property
    def result_lines(self) -> bool:
        """True when tool result bodies are echoed to the console."""
        return self.verbosity == "full"

    def set_verbosity(self, level: str) -> None:
        """Change how much the console shows (the conversation is untouched).

        The module-level flags are what the rendering helpers consult, so they
        are updated here too; the Harness attribute drives the per-call choice.
        """
        level = (level or "").strip().lower()
        if level not in VERBOSITY_LEVELS:
            raise ValueError("usage: /verbose [full|brief|quiet]")
        self.verbosity = apply_verbosity(level)
        if level == "full":
            shown = "tool results as before"
        elif level == "brief":
            shown = "tool headline + one result line, no command output"
        else:
            shown = "tool headline only (warnings and errors still shown)"
        print(notice("ok", f"verbosity {level} - {shown}"))

    # -- paid / free profiles ---------------------------------------------- #

    def _infer_free(self) -> bool:
        """Decide the startup tier from the model name, then the keys present.

        An explicitly chosen paid model starts paid, and an explicit free-tier
        model (one ending in ":free") starts free. When the model is left at
        the default, prefer whichever tier actually has a key set.
        """
        if self.model == DEFAULT_MODEL:
            has_free = bool(os.environ.get(FREE_API_KEY_ENV))
            has_paid = bool(os.environ.get(PAID_API_KEY_ENV))
            # Prefer the tier that has a key set; free is the fallback.
            return not (has_paid and not has_free)
        return self.model == FREE_MODEL or self.model.endswith(":free")

    def _profile(self, free: bool) -> tuple[str, str, str]:
        """Return (model, base_url, api_key) for the chosen tier."""
        if free:
            key = self.api_key or os.environ.get(FREE_API_KEY_ENV)
            return FREE_MODEL, FREE_BASE_URL, key
        key = self.api_key or os.environ.get(PAID_API_KEY_ENV)
        return PAID_MODEL, PAID_BASE_URL, key

    def _build_client(self) -> OpenAI:
        model, base_url, key = self._profile(self.free)
        self.model = model
        return OpenAI(api_key=key or "not-set", base_url=base_url)

    def set_free(self, enabled: bool) -> None:
        """Swap the client between the free and paid profiles."""
        free = bool(enabled)
        model, base_url, key = self._profile(free)
        if not key:
            env = FREE_API_KEY_ENV if free else PAID_API_KEY_ENV
            raise ValueError(
                f"{env} is not set - export it, then try /model "
                f"{'free' if free else 'paid'} again"
            )
        self.free = free
        self.client = self._build_client()
        tier = "free" if self.free else "paid"
        print(notice(
            "ok",
            f"now using the {tier} model: {model} at {base_url}",
        ))

    # -- skills ------------------------------------------------------------ #

    def system_prompt(self) -> str:
        """The system turn: base rules, the skill rules, and the skill index.

        The base rules follow the mode - a coder is told to change the code, a
        tester is told never to.
        """
        base = TESTER_SYSTEM_PROMPT if self.tester else SYSTEM_PROMPT
        text = base + SKILL_SYSTEM_PROMPT
        index = self.skills.index()
        if index:
            text += f"\nAvailable skills (load one with @{{name}}):\n{index}\n"
        return text

    def refresh_system_prompt(self) -> None:
        """Pick up skills added since startup without losing the conversation."""
        self.messages[0] = {"role": "system", "content": self.system_prompt()}

    # -- tester mode -------------------------------------------------------- #

    def set_tester(self, enabled: bool) -> None:
        """Switch between the coder and the tester halves of the loop.

        The flag is not only a prompt: the write tools are narrowed to the
        testing report and delete_file is dropped, so the tool menu and the
        system turn are rebuilt here as well. The conversation itself is
        kept - only messages[0] changes.
        """
        self.tester = bool(enabled)
        self.code_read_only = self.tester
        self.tools = tools_for(self.tester)
        self.messages[0] = {"role": "system", "content": self.system_prompt()}
        if self.tester:
            print(notice(
                "ok",
                f"tester mode on - writes go only to "
                f"{REPORT_PATH.as_posix()} and nothing is deleted",
            ))
        else:
            print(notice(
                "ok",
                "tester mode off - the coder tools are back, writes are real",
            ))

    def load_skills(self, text: str) -> list[Skill]:
        """Pull every @{skill} referenced in `text` into the conversation.

        The skill text is queued as a message *before* the prompt that asked
        for it, so the model reads the instructions first. Returns the skills
        that were newly added; unknown names and skills already in the context
        are reported but not sent again.
        """
        names = referenced_skills(text)
        if not names:
            return []

        loaded: list[Skill] = []
        missing: list[str] = []
        for name in names:
            skill = self.skills.find(name)
            if skill is None:
                missing.append(name)
            elif self.loaded_skills.get(skill.name) != skill.body:
                self.loaded_skills[skill.name] = skill.body
                loaded.append(skill)

        if loaded or missing:
            entry = {
                "role": "user",
                "content": skill_context(loaded, missing, self.skills),
            }
            self.messages.append(entry)
            self._log(entry)
        for skill in loaded:
            print(
                notice("skill", f"loaded @{{{skill.name}}} from "
                                f"{self.skills.rel(skill.path)}", stream=sys.stderr),
                file=sys.stderr,
            )
        for name in missing:
            print(
                notice("warn", f"no skill named @{{{name}}} in "
                               f"{self.skills.describe_roots()} - /skills lists them",
                       stream=sys.stderr),
                file=sys.stderr,
            )
        return loaded

    # -- path handling ----------------------------------------------------- #

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        """True when `path` is `root` itself or lives underneath it."""
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    def read_roots(self) -> list[Path]:
        """Directories the read-only tools may look at, workspace first.

        The workspace is always readable and writable. A skills directory that
        lives outside it - say ~/.config/uharness/skills - is readable too, so
        `read_file` can fetch a skill's bundled reference files. Skills are
        data the user installed on purpose, so this grant is narrow: it covers
        the skill directories only, never their parents, and it never allows a
        write.
        """
        roots = [self.workspace]
        for root in self.skills.external_roots():
            if root not in roots:
                roots.append(root)
        return roots

    def resolve(self, filename: str, *, write: bool = False) -> Path:
        """Turn a tool-supplied path into a safe absolute one.

        Reads are allowed inside the workspace and inside any external skill
        directory. Writes (`write=True`) are allowed inside the workspace only,
        so a skill installed elsewhere can be read but never modified.
        """
        if not filename or not filename.strip():
            raise ToolError("filename must not be empty")
        candidate = Path(filename).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()

        if self._inside(resolved, self.workspace):
            return resolved
        if not write:
            for root in self.read_roots()[1:]:
                if self._inside(resolved, root):
                    return resolved
            raise ToolError(
                f"path '{filename}' is outside the workspace ({self.workspace}) and "
                "outside every skill directory; use a path inside the workspace"
            )
        raise ToolError(
            f"path '{filename}' is outside the workspace ({self.workspace}); "
            "changes must be written inside it"
        )

    def resolve_write(self, filename: str) -> Path:
        """A path that may be modified: the workspace, nothing else.

        In tester mode the workspace is not enough: only the testing report
        may be written, because a tester reports defects and never fixes them.
        The refusal names the report, so the model knows where its finding
        belongs instead of trying another path.
        """
        path = self.resolve(filename, write=True)
        if self.code_read_only and path != self.report_path:
            raise ToolError(
                f"'{filename}' is the code under test, and this is a tester run: "
                f"the only file it may write is {REPORT_PATH.as_posix()}. "
                "Write the problem up there (or report it in chat) instead of "
                "changing the code."
            )
        return path

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace)) or "."
        except ValueError:
            return str(path)

    def _iter_files(self, root: Path):
        """Yield regular files under `root`, skipping ignored directories."""
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")
            )
            for name in sorted(filenames):
                if name.startswith(".") and name not in {".env", ".gitignore"}:
                    continue
                yield Path(dirpath) / name

    # -- approval ---------------------------------------------------------- #
    #
    # Asking is the default, but the mode must be changeable at *any* time -
    # including from inside the allow? prompt, because that is the only thing
    # the user can reach while an agent loop is still running. Typing
    # "always" (or "/approve on") there answers the current call and stops the
    # questions for the rest of the session, so a forgotten --yes no longer
    # means pressing y for every remaining tool call.

    ALWAYS_ANSWERS: ClassVar[set[str]] = {"a", "always", "all", "on", "yes-all"}
    ASK_ANSWERS: ClassVar[set[str]] = {"off", "ask"}

    def set_auto_approve(self, enabled: bool) -> None:
        """Turn asking on or off. Safe anywhere: prompt, command, mid-loop."""
        self.auto_approve = bool(enabled)
        if self.tester:
            on = "auto-approval on - report writes and commands run without asking"
            off = ("auto-approval off - asking before writing the report and "
                   "before running commands")
        else:
            on = "auto-approval on - write/edit/delete/execute run without asking"
            off = "auto-approval off - asking before write/edit/delete/execute"
        if self.auto_approve:
            extra = "" if self.allow_dangerous else " (dangerous commands still blocked)"
            print(notice("ok", f"{on}{extra}"))
        else:
            print(notice("ok", off))

    def _approval_command(self, answer: str) -> bool | None:
        """Apply a /approve command typed at the prompt; None if it is not one."""
        word, _, argument = answer.partition(" ")
        if word not in {"/approve", "/auto"}:
            return None
        argument = argument.strip()
        if argument in {"", "toggle"}:
            self.set_auto_approve(not self.auto_approve)
        elif argument == "on":
            self.set_auto_approve(True)
        elif argument == "off":
            self.set_auto_approve(False)
        else:
            print(notice("warn", "usage: /approve [on|off] - blank toggles",
                         stream=sys.stderr), file=sys.stderr)
            return None
        return self.auto_approve

    def _approve(self, action: str, detail: str, *, dangerous: bool = False) -> bool:
        if dangerous and not self.allow_dangerous:
            return False
        while True:
            # Re-checked on every pass: the mode can flip while a loop runs.
            if self.auto_approve:
                return True
            if not sys.stdin.isatty():
                print(
                    notice("deny", f"{action} requires confirmation; rerun with --yes"),
                    file=sys.stderr,
                )
                return False
            print()
            # The detail is where a shell command lands, so it folds under
            # itself instead of running past the edge: the whole command has
            # to be readable before it can be approved.
            print(approval_prompt(action, detail))
            try:
                answer = read_line(
                    "  " + paint("allow?", "bold", "yellow")
                    + paint(" [y/N] ", "dim")
                    + paint("always", "bold", "cyan")
                    + paint(" to stop asking: ", "dim")
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False

            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no", ""}:
                return False
            if answer in self.ALWAYS_ANSWERS:
                self.set_auto_approve(True)
                return True
            if answer in self.ASK_ANSWERS:
                self.set_auto_approve(False)
                continue  # same call is asked about again
            decided = self._approval_command(answer)
            if decided is not None:
                if decided:
                    return True
                continue  # /approve off: keep asking, starting with this call
            print(notice(
                "warn",
                "answer y, n or always - /approve on and /approve off work here too",
                stream=sys.stderr,
            ), file=sys.stderr)

    # -- tool implementations --------------------------------------------- #

    def tool_list_files(self, path: str = ".", recursive: bool = False) -> str:
        target = self.resolve(path)
        if not target.exists():
            raise ToolError(f"directory not found: {path}")
        if not target.is_dir():
            raise ToolError(f"not a directory: {path}")

        entries: list[str] = []
        truncated = False

        if recursive:
            for item in self._iter_files(target):
                entries.append(
                    self.rel(item) + (f"  ({item.stat().st_size} bytes)")
                )
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break
        else:
            items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            for item in items:
                suffix = "/" if item.is_dir() else f"  ({item.stat().st_size} bytes)"
                entries.append(item.name + suffix)
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break

        if truncated:
            entries.append(f"... [list truncated after {MAX_LIST_ENTRIES} entries]")
        return f"{self.rel(target)}:\n" + ("\n".join(entries) if entries else "(empty)")

    def tool_search_files(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        case_sensitive: bool = True,
        max_results: int | None = None,
    ) -> str:
        if not pattern:
            raise ToolError("pattern must not be empty")
        try:
            regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from None

        root = self.resolve(path or ".")
        if not root.exists():
            raise ToolError(f"not found: {path}")

        limit = min(max(int(max_results or MAX_SEARCH_RESULTS), 1), MAX_SEARCH_RESULTS * 5)
        glob_re = None
        if glob:
            glob_re = re.compile(
                "^" + re.escape(glob).replace(r"\*", ".*").replace(r"\?", ".") + "$"
            )

        files = [root] if root.is_file() else list(self._iter_files(root))
        hits: list[str] = []
        scanned = skipped_binary = skipped_glob = skipped_big = 0
        truncated = False

        for file_path in files:
            if glob_re is not None and not glob_re.match(file_path.name):
                skipped_glob += 1
                continue
            try:
                if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    skipped_big += 1
                    continue
                with file_path.open("rb") as handle:
                    if b"\0" in handle.read(4096):
                        skipped_binary += 1
                        continue
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            scanned += 1
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(
                        f"{self.rel(file_path)}:{lineno}: "
                        f"{line.strip()[:MAX_LINE_PREVIEW]}"
                    )
                    if len(hits) >= limit:
                        truncated = True
                        break
            if truncated:
                break

        header = f"{len(hits)} match(es) for /{pattern}/ in {self.rel(root)}"
        notes = []
        if truncated:
            notes.append(f"stopped at the {limit}-match limit")
        if skipped_glob:
            notes.append(f"{skipped_glob} file(s) excluded by glob '{glob}'")
        if skipped_binary:
            notes.append(f"{skipped_binary} binary file(s) skipped")
        if skipped_big:
            notes.append(f"{skipped_big} file(s) over {MAX_SEARCH_FILE_BYTES} bytes skipped")
        if not hits:
            notes.append(f"searched {scanned} text file(s)")
        if notes:
            header += " (" + "; ".join(notes) + ")"
        body = "\n".join(hits) if hits else "(no matches)"
        return clip(f"{header}\n{body}", MAX_TOOL_OUTPUT)

    def tool_read_file(
        self, filename: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        path = self.resolve(filename)
        if not path.exists():
            raise ToolError(f"file not found: {filename}")
        if path.is_dir():
            raise ToolError(f"'{filename}' is a directory; use list_files instead")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(
                f"'{filename}' is not UTF-8 text; use execute_command with a tool like `file`"
            ) from None
        except OSError as exc:
            raise ToolError(f"could not read '{filename}': {exc}") from None

        lines = text.splitlines()
        total = len(lines)
        first = max((start_line or 1), 1)
        last = min(end_line if end_line is not None else total, total)
        if first > total:
            raise ToolError(f"start_line {first} is beyond the end of the file ({total} lines)")
        selected = lines[first - 1:last]
        if len(selected) > MAX_READ_LINES:
            selected = selected[:MAX_READ_LINES]
            last = first + MAX_READ_LINES - 1

        header = f"{self.rel(path)} (lines {first}-{last} of {total})"
        body = "\n".join(selected)
        full = f"{header}:\n{body}"
        if len(full) > MAX_FILE_CONTENT:
            return (
                clip(full, MAX_FILE_CONTENT, keep="head")
                + f"\n[content cut off - re-read with start_line/end_line past line {last}]"
            )
        if last < total:
            full += f"\n[file continues: {total - last} more line(s); read them with start_line={last + 1}]"
        return full

    def tool_edit_file(
        self,
        filename: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        path = self.resolve_write(filename)
        if not path.exists():
            raise ToolError(f"file not found: {filename}")
        if path.is_dir():
            raise ToolError(f"'{filename}' is a directory; use write_file instead")
        if old_string == "":
            raise ToolError("old_string must not be empty; use write_file to replace a whole file")

        try:
            # newline="" keeps the file's real line endings; they are restored
            # on write so a CRLF file is not silently rewritten to LF.
            with path.open("r", encoding="utf-8", newline="") as handle:
                raw = handle.read()
        except UnicodeDecodeError:
            raise ToolError(f"'{filename}' is not UTF-8 text") from None
        except OSError as exc:
            raise ToolError(f"could not read '{filename}': {exc}") from None

        uses_crlf = "\r\n" in raw
        before = raw.replace("\r\n", "\n")

        count = before.count(old_string)
        if count == 0:
            first_line = before.splitlines()[0][:80] if before.strip() else "(file is empty)"
            raise ToolError(
                f"old_string not found in {filename}. It must match exactly, "
                f"including whitespace and indentation. Re-read the file and "
                f"copy the text verbatim (first line is: {first_line!r})."
            )
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_string occurs {count} times in {filename}, so the edit "
                "would be ambiguous. Include more surrounding lines to make it "
                "unique, or pass replace_all=true to change all of them."
            )

        after = before.replace(old_string, new_string, count if replace_all else 1)
        if after == before:
            return f"No change: {filename} already contains that text"

        if self.dry_run:
            return f"DRY RUN: would edit {filename}\n{make_diff(before, after, self.rel(path))}"

        preview = f"{short(old_string, 100)} -> {short(new_string, 100)}"
        if not self._approve("Edit file", f"{self.rel(path)}: {preview}"):
            raise ToolError(f"user denied permission to edit '{filename}'")

        self._snapshot(path)
        output = after.replace("\n", "\r\n") if uses_crlf else after
        try:
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(output)
        except OSError as exc:
            raise ToolError(f"could not write '{filename}': {exc}") from None

        changed = count if replace_all else 1
        return (
            f"Edited {self.rel(path)} ({changed} replacement(s))\n"
            + make_diff(before, after, self.rel(path))
        )

    def tool_write_file(self, filename: str, content: str) -> str:
        path = self.resolve_write(filename)
        exists = path.exists()
        if exists and path.is_dir():
            raise ToolError(f"'{filename}' is a directory")
        action = "Overwrite" if exists else "Create"
        if self.dry_run:
            return f"DRY RUN: would {action.lower()} {self.rel(path)} ({len(content)} characters)"
        if not self._approve(f"{action} file", self.rel(path)):
            raise ToolError(f"user denied permission to write '{filename}'")
        self._snapshot(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"could not write '{filename}': {exc}") from None
        lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        verb = "Overwrote" if exists else "Created"
        return f"{verb} {self.rel(path)} ({len(content.encode('utf-8'))} bytes, {lines} lines)"

    def tool_append_file(self, filename: str, content: str) -> str:
        path = self.resolve_write(filename)
        if self.dry_run:
            return f"DRY RUN: would append {len(content)} characters to {self.rel(path)}"
        if not self._approve("Append to file", self.rel(path)):
            raise ToolError(f"user denied permission to append to '{filename}'")
        self._snapshot(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise ToolError(f"could not append to '{filename}': {exc}") from None
        return f"Appended {len(content.encode('utf-8'))} bytes to {self.rel(path)}"

    def tool_delete_file(self, filename: str, recursive: bool = False) -> str:
        if self.code_read_only:
            raise ToolError(
                "this is a tester run: nothing is deleted, not even the report. "
                f"Write findings to {REPORT_PATH.as_posix()} instead."
            )
        path = self.resolve_write(filename)
        if path == self.workspace:
            raise ToolError("refusing to delete the workspace root")
        if not path.exists():
            raise ToolError(f"not found: {filename}")

        is_dir = path.is_dir()
        entry_count = 0
        if is_dir:
            try:
                entry_count = len(list(path.iterdir()))
            except OSError:
                entry_count = 0
            if entry_count and not recursive:
                raise ToolError(
                    f"'{filename}' is a non-empty directory ({entry_count} entries); "
                    "pass recursive=true to remove it and everything in it"
                )

        label = self.rel(path) + (" (recursive)" if is_dir and entry_count else "")
        if self.dry_run:
            return f"DRY RUN: would delete {label}"
        if not self._approve("Delete", label):
            raise ToolError(f"user denied permission to delete '{filename}'")

        self._snapshot(path)
        try:
            if is_dir:
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            raise ToolError(f"could not delete '{filename}': {exc}") from None
        suffix = f" ({entry_count} entries)" if is_dir and entry_count else ""
        return f"Deleted {self.rel(path)}{suffix}"

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """Kill the command and any children it started.

        On POSIX the command runs in its own session (see start_new_session
        below), so killing the process group takes the whole tree with it. On
        Windows there are no process groups to signal, so taskkill /T walks the
        child tree instead. Both fall back to a plain kill if that fails, so a
        timeout can never leave an orphaned process behind.
        """
        killed = False
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                killed = True
            except (OSError, ProcessLookupError):
                pass  # no group to signal (or it already exited): try the pid
        elif os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                killed = True
            except OSError:
                pass
        if not killed:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def tool_execute_command(self, command: str, timeout: int | None = None) -> str:
        if not command.strip():
            raise ToolError("command must not be empty")

        dangerous_reason = next(
            (why for regex, why in DANGEROUS_RE if regex.search(command)), None
        )
        if dangerous_reason and not self.allow_dangerous:
            raise ToolError(
                f"blocked: command looks dangerous ({dangerous_reason}). "
                "The user must rerun with --allow-dangerous to permit it."
            )

        limit = min(max(int(timeout or self.command_timeout), 1), MAX_COMMAND_TIMEOUT)
        if self.dry_run:
            return f"DRY RUN: would run with a {limit}s timeout: {command}"
        if not self._approve("Run command", command, dangerous=bool(dangerous_reason)):
            raise ToolError("user denied permission to run the command")

        # The command sits on a grey band so a long run of tool calls still
        # shows at a glance where one command ends and the next begins.
        print(notice("run", command, background=band_background()))
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=self.workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                errors="replace",
                # POSIX: a new session so a timeout can kill the whole group.
                # Windows: no sessions, so _kill_tree uses taskkill /T instead.
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ToolError(f"could not run command: {exc}") from None

        out = BoundedBuffer(MAX_COMMAND_CAPTURE)
        err = BoundedBuffer(MAX_COMMAND_CAPTURE)
        show_progress = sys.stdout.isatty()
        progress = [0]

        def pump(pipe, buffer: BoundedBuffer) -> None:
            try:
                for line in pipe:
                    buffer.add(line)
                    # A dot per line is enough to show liveness on long builds.
                    if show_progress and progress[0] < 60:
                        progress[0] += 1
                        sys.stdout.write(".")
                        sys.stdout.flush()
            except (OSError, ValueError):
                pass
            finally:
                try:
                    pipe.close()
                except OSError:
                    pass

        threads = [
            threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, err), daemon=True),
        ]
        for thread in threads:
            thread.start()

        deadline = time.monotonic() + limit
        timed_out = False
        try:
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._kill_tree(proc)
                    break
                time.sleep(0.05)
        except KeyboardInterrupt:
            # Ctrl+C stops the loop; never leave the command running behind us.
            self._kill_tree(proc)
            raise
        for thread in threads:
            thread.join(timeout=5)
        if show_progress and progress[0]:
            print()

        exit_code = proc.poll()
        if exit_code is None:
            exit_code = -9

        if timed_out:
            partial = (
                f"timed out after {limit}s (process killed)\n"
                f"exit_code: {exit_code}\n"
                f"stdout:\n{out.value().rstrip() or '(empty)'}\n"
                f"stderr:\n{err.value().rstrip() or '(empty)'}\n"
                "hint: the command may need a longer `timeout`, or it is waiting "
                "for input that never arrives"
            )
            return clip(partial, MAX_TOOL_OUTPUT)
        return format_result(out.value(), err.value(), exit_code)

    # -- dispatch ---------------------------------------------------------- #

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        handlers: dict[str, collections.abc.Callable[..., str]] = {
            "list_files": self.tool_list_files,
            "search_files": self.tool_search_files,
            "read_file": self.tool_read_file,
            "edit_file": self.tool_edit_file,
            "write_file": self.tool_write_file,
            "append_file": self.tool_append_file,
            "delete_file": self.tool_delete_file,
            "execute_command": self.tool_execute_command,
        }
        # delete_file is not advertised in tester mode, but an older
        # conversation or a hand-written call can still name it: the method
        # refuses there, so route it rather than answering "unknown tool".
        if self.tester:
            handlers["delete_file"] = self.tool_delete_file
        handler = handlers.get(name)
        if handler is None:
            return f"ERROR: unknown tool '{name}'"
        try:
            return handler(**args)
        except ToolError as exc:
            return f"ERROR: {exc}"
        except TypeError as exc:
            return f"ERROR: bad arguments for {name}: {exc}"
        except Exception as exc:  # keep the agent alive on unexpected failures
            return f"ERROR: {name} failed: {exc}"

    # -- context management ------------------------------------------------ #

    def _compress_history(self) -> None:
        """Replace old tool output with stubs so long sessions keep working.

        Assistant/user turns are never touched - they carry the plan and the
        task - only bulky tool results that the model has already used.
        """
        if MAX_HISTORY_CHARS <= 0:
            return
        total = sum(len(str(message.get("content") or "")) for message in self.messages)
        if total <= MAX_HISTORY_CHARS:
            return

        tool_indices = [
            index for index, message in enumerate(self.messages)
            if message.get("role") == "tool"
        ]
        candidates = (
            tool_indices[:-KEEP_RECENT_TOOL_RESULTS]
            if KEEP_RECENT_TOOL_RESULTS > 0
            else tool_indices
        )

        freed = 0
        for index in candidates:
            content = str(self.messages[index].get("content") or "")
            if not content or content.startswith("[dropped]"):
                continue
            self.messages[index]["content"] = (
                f"[dropped] {len(content)} characters of earlier tool output "
                "were removed to fit the context window. Re-run the tool if you "
                "still need that information."
            )
            freed += len(content)
            total -= len(content)
            if total <= MAX_HISTORY_CHARS * 0.7:
                break

        if freed and not self._compaction_noted:
            self._compaction_noted = True
            print(
                notice("context", f"dropped {freed} characters of old tool output",
                       stream=sys.stderr),
                file=sys.stderr,
            )

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is not None:
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = getattr(usage, key, None)
                if isinstance(value, int):
                    self.usage[key] = self.usage.get(key, 0) + value
        self.api_calls += 1

    def _report_usage(self) -> None:
        if not self.show_usage or not self.usage:
            return
        print(
            notice(
                "usage",
                f"calls {self.api_calls}"
                f"   prompt {self.usage.get('prompt_tokens', 0)}"
                f"   completion {self.usage.get('completion_tokens', 0)}"
                f"   total {self.usage.get('total_tokens', 0)} tokens",
                stream=sys.stderr,
            ),
            file=sys.stderr,
        )

    # -- API --------------------------------------------------------------- #

    def _chat(self, use_tools: bool = True) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "stream": False,
        }
        if use_tools:
            kwargs["tools"] = self.tools
            kwargs["tool_choice"] = "auto"
        if self.thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["reasoning_effort"] = "high"

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self.client.chat.completions.create(**kwargs)
                self._record_usage(response)
                return response
            except (RateLimitError, APIConnectionError) as exc:
                last_error = exc
            except APIStatusError as exc:
                if exc.status_code < 500:
                    raise
                last_error = exc
            if attempt < 3:
                wait = 2 ** attempt
                print(
                    notice(
                        "retry",
                        f"API error, retrying in {wait}s "
                        f"({attempt}/2)",
                        stream=sys.stderr,
                    ),
                    file=sys.stderr,
                )
                time.sleep(wait)
        assert last_error is not None
        raise last_error

    # -- logging ----------------------------------------------------------- #

    def _log(self, entry: dict[str, Any]) -> None:
        if not self.log_path:
            return
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(notice("warn", f"could not write log: {exc}", stream=sys.stderr),
                  file=sys.stderr)

    # -- main turn loop ---------------------------------------------------- #

    def _summarise_after_limit(self) -> str:
        """One tool-free call so hitting --max-turns still ends usefully."""
        self.messages.append({
            "role": "user",
            "content": (
                "You have reached the tool-call limit for this request. Do not "
                "call any more tools. Summarise: what you completed, which files "
                "you created or changed, what is unfinished, and any blockers."
            ),
        })
        text = ""
        try:
            response = self._chat(use_tools=False)
            text = response.choices[0].message.content or ""
        except Exception as exc:
            print(
                notice("warn", f"could not get a closing summary: {exc}",
                       stream=sys.stderr),
                file=sys.stderr,
            )
        if text.strip():
            self.messages.append({"role": "assistant", "content": text})
            begin_answer()
            print("\n" + assistant_block(text.strip(), final=True))
            return text
        return (
            f"Stopped after {self.max_turns} turns without a final answer. "
            "Increase --max-turns if the task needs more steps."
        )

    def run(self, user_message: str) -> str:
        """Answer one user message, autosaving at every step on the way.

        The final save sits in a finally block, so an API error, a Ctrl+C or
        hitting --max-turns still leaves the conversation on disk exactly as
        far as it got - a long run is never lost just because it did not end
        tidily.
        """
        try:
            return self._run_turn(user_message)
        finally:
            self.autosave_tick()

    def _run_turn(self, user_message: str) -> str:
        # Pull any @{skill} the prompt references into the conversation first,
        # so the skill text sits *before* the request that asked for it. This
        # also refreshes the system prompt, so a skill added to disk since
        # startup appears in the index straight away.
        # One /undo reverts one request, so each turn starts a fresh group.
        self._undo_group = None
        self.refresh_system_prompt()
        self.load_skills(user_message)

        user_entry = {"role": "user", "content": user_message}
        self.messages.append(user_entry)
        self._log(user_entry)
        # Checkpoint the request before the first API call: if the process is
        # killed while the model is thinking, the prompt itself still survives.
        self.autosave_tick()

        for _turn in range(1, self.max_turns + 1):
            self._compress_history()
            try:
                response = self._chat()
            except APIError as exc:
                message = f"API error: {exc}"
                print(notice("error", message, stream=sys.stderr), file=sys.stderr)
                self._report_usage()
                return message
            except KeyboardInterrupt:
                message = "interrupted"
                print(notice("warn", "interrupted - request cancelled", stream=sys.stderr),
                      file=sys.stderr)
                self._report_usage()
                return message
            except Exception as exc:
                message = f"Unexpected API error: {exc}"
                print(notice("error", message, stream=sys.stderr), file=sys.stderr)
                self._report_usage()
                return message

            message = response.choices[0].message
            content = message.content or ""
            tool_calls = list(message.tool_calls or [])

            assistant_entry: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in tool_calls
                ]
            self.messages.append(assistant_entry)
            self._log(assistant_entry)

            if content.strip():
                # Prose that comes with a tool call is commentary; the block
                # with no calls after it is the answer to the request, and it
                # is the only one drawn in ANSWER_COLOUR.
                final = not tool_calls
                if final:
                    begin_answer()
                print("\n" + assistant_block(content.strip(), final=final))

            if not tool_calls:
                self._report_usage()
                return content

            answered: set[str] = set()
            try:
                for call in tool_calls:
                    name = call.function.name
                    raw_args = call.function.arguments or "{}"
                    print("\n" + tool_call_block(name, raw_args))
                    try:
                        args = json.loads(raw_args)
                        if not isinstance(args, dict):
                            raise ValueError("arguments must be a JSON object")
                    except (json.JSONDecodeError, ValueError) as exc:
                        result = f"ERROR: invalid JSON arguments: {exc}"
                    else:
                        result = self.dispatch(name, args)

                    print(result_block(result, summary_only=not self.result_lines))
                    tool_entry = {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    }
                    self.messages.append(tool_entry)
                    self._log(tool_entry)
                    answered.add(call.id)
            except KeyboardInterrupt:
                # Ctrl+C mid-loop: stop here, but give every announced tool_call
                # a result - the API rejects a dangling call on the next turn.
                print()
                print(notice(
                    "warn",
                    "interrupted - stopping this request; /approve off now works "
                    "again",
                    stream=sys.stderr,
                ), file=sys.stderr)
                for call in tool_calls:
                    if call.id in answered:
                        continue
                    tool_entry = {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": "ERROR: interrupted by the user before this "
                                   "tool call finished",
                    }
                    self.messages.append(tool_entry)
                    self._log(tool_entry)
                self._report_usage()
                return "interrupted"

            # Turn finished: push the conversation to disk before asking the
            # model for the next step, so a crash mid-task resumes from the
            # last tool result instead of from the start.
            self.autosave_tick()

        summary = self._summarise_after_limit()
        self._report_usage()
        return summary

    def reset(self) -> None:
        self.messages = [self.messages[0]]

    # -- saved conversations ------------------------------------------------ #
    #
    # /save drops the whole message list in .harness_sessions/<name>.json and
    # /open puts it back. The directory is hidden and starts with a dot, so the
    # agent's own list_files/search_files never trip over saved chats.

    def _session_path(self, name: str) -> Path:
        """Map a user-supplied session name onto a file inside sessions_dir."""
        cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", name)
        cleaned = re.sub(r"\s+", "-", cleaned)
        # Collapse dot runs ("..", "...") so a name can never look like a
        # parent-directory reference, then trim the decorative edges.
        cleaned = re.sub(r"\.{2,}", ".", cleaned)
        cleaned = cleaned.strip(" .-")[:MAX_SESSION_NAME]
        if not cleaned:
            raise ValueError("session name needs at least one letter or digit")
        return self.sessions_dir / f"{cleaned}.json"

    def save_session(self, name: str | None = None) -> Path:
        """Write the conversation to disk and return the file it went to."""
        if not name or not name.strip():
            name = time.strftime("chat-%Y%m%d-%H%M%S")
        path = self._session_path(name)
        payload = {
            "version": 1,
            "name": path.stem,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": self.model,
            "workspace": str(self.workspace),
            "messages": self.messages,
        }
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp.replace(path)  # atomic: a failed save never eats the old file
        except OSError:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
        self.last_session_name = path.stem   # autosave keeps updating this file
        return path

    def list_sessions(self) -> list[dict[str, Any]]:
        """Saved conversations, newest first. Unreadable files are skipped."""
        if not self.sessions_dir.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for path in sorted(self.sessions_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                modified = path.stat().st_mtime
            except (OSError, ValueError):
                continue
            messages = data.get("messages") if isinstance(data, dict) else None
            if not isinstance(messages, list):
                continue
            found.append({
                "name": path.stem,
                "path": path,
                "saved_at": str(
                    data.get("saved_at")
                    or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(modified))
                ),
                "model": str(data.get("model") or "?"),
                "messages": len(messages),
                "preview": session_preview(messages),
                "modified": modified,
            })
        found.sort(key=lambda item: (item["modified"], item["name"]), reverse=True)
        return found

    def load_session(self, selector: str) -> dict[str, Any]:
        """Replace the current history with a saved one (number, name or index).

        The live system prompt is kept, so a session saved by an older version
        of the harness still runs with today's instructions.
        """
        sessions = self.list_sessions()
        if not sessions:
            raise ValueError(f"no saved conversations in {self.rel(self.sessions_dir)}")
        text = selector.strip()
        chosen: dict[str, Any] | None = None
        if text.isdigit():
            index = int(text)
            if not 1 <= index <= len(sessions):
                raise ValueError(f"no conversation {index}; pick 1-{len(sessions)}")
            chosen = sessions[index - 1]
        else:
            for item in sessions:
                if item["name"].lower() == text.lower():
                    chosen = item
                    break
        if chosen is None:
            raise ValueError(f"no saved conversation named '{selector}'")

        try:
            data = json.loads(chosen["path"].read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"could not read {chosen['name']}: {exc}") from None
        raw = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise ValueError(f"{chosen['name']} does not contain a message list")

        restored = [
            message for message in raw
            if isinstance(message, dict)
            and message.get("role")
            and message.get("role") != "system"
        ]
        self.messages = [self.messages[0], *restored]
        self._compaction_noted = False
        self.last_session_name = chosen["name"]   # autosave writes back here
        chosen["restored"] = len(restored)
        return chosen

    def autosave_session(self) -> Path | None:
        """Save the conversation on the way out; None if there is nothing to keep.

        A conversation that never got past the system prompt is not written, so
        opening the harness and quitting straight away leaves no clutter. When
        the run already used /save or /open, that same file is updated, so
        resuming a chat and quitting again does not spawn a copy every time.
        """
        if len(self.messages) <= 1:
            return None
        return self.save_session(self.last_session_name)

    def autosave_tick(self) -> None:
        """Write the conversation mid-run so nothing is lost if the run dies.

        run() calls this after every turn, and once more when the request ends
        however it ends. It stays quiet: the single "autosaved ..." line comes
        from the exit path, and a full disk should not print a warning per
        turn, so the first failure is reported and the rest are swallowed.
        """
        if not self.autosave:
            return
        try:
            self.autosave_session()
        except (OSError, ValueError) as exc:
            if self._autosave_warned:
                return
            self._autosave_warned = True
            print(
                notice("warn", f"could not autosave: {exc} (continuing)",
                       stream=sys.stderr),
                file=sys.stderr,
            )

    # -- undo --------------------------------------------------------------- #
    #
    # The writing tools are destructive by design: write_file replaces a whole
    # file, delete_file can remove a tree, and edit_file is a blind string
    # substitution. Before any of them touches a path, _snapshot() copies what
    # is there into .harness_sessions/undo/<stamp>/ and records it in that
    # group's manifest. /undo then puts the last request's changes back. A
    # tester run can only reach the testing report, so there undo covers the
    # report too - a mangled handover is one /undo away.
    #
    # One group per request, not per tool call: an agent turn is the unit a
    # human thinks in ("undo what it just did"), and a ten-call refactor should
    # be one undo away, not ten. A file touched twice in one turn is snapshotted
    # once - the state that matters is the one from before the turn started.

    UNDO_DIR_NAME = "undo"
    UNDO_KEEP = 10            # snapshot groups kept; older ones are pruned

    def undo_root(self) -> Path:
        """Where snapshot groups live: inside the workspace, and dot-hidden."""
        return self.sessions_dir / self.UNDO_DIR_NAME

    def _undo_group_path(self) -> Path:
        """The group for the current turn, created on first use."""
        if self._undo_group is not None:
            return self._undo_group
        root = self.undo_root()
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        candidate = root / stamp
        suffix = 1
        # Two turns in the same second must not share a group.
        while candidate.exists():
            candidate = root / f"{stamp}-{suffix:03d}"
            suffix += 1
        candidate.mkdir()
        self._undo_group = candidate
        # Directory names are not creation order: pruning frees names, so a
        # later turn can reuse an older, alphabetically-earlier one. Record a
        # monotonic sequence instead and sort by that everywhere.
        seq = max((self._group_seq(path) for path in self._all_group_dirs()),
                  default=0) + 1
        (candidate / "seq").write_text(f"{seq}\n", encoding="utf-8")
        self._prune_undo_groups()
        return candidate

    def _all_group_dirs(self) -> list[Path]:
        root = self.undo_root()
        if not root.is_dir():
            return []
        return [path for path in root.iterdir() if path.is_dir()]

    @staticmethod
    def _group_seq(group: Path) -> int:
        """A group's creation order; 0 for anything written without one."""
        try:
            return int((group / "seq").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _prune_undo_groups(self) -> None:
        """Keep the newest UNDO_KEEP groups, drop the rest."""
        groups = sorted(self._all_group_dirs(),
                        key=lambda path: (self._group_seq(path), path.name))
        for old in groups[:max(0, len(groups) - self.UNDO_KEEP)]:
            if old == self._undo_group:
                continue           # never prune the group this turn is writing
            try:
                shutil.rmtree(old)
            except OSError:
                pass

    @staticmethod
    def _read_manifest(group: Path) -> list[dict[str, Any]]:
        """The entries recorded in one group, oldest first."""
        try:
            data = json.loads((group / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = data.get("entries") if isinstance(data, dict) else None
        return [entry for entry in entries or [] if isinstance(entry, dict)]

    def _snapshot(self, path: Path) -> None:
        """Remember `path` as it is now, before a tool changes it.

        Called after approval and before the write, so a denied or failed tool
        call records nothing. A path that does not exist yet is recorded as
        kind "none", which is what lets /undo delete a file the agent created.
        """
        if self.dry_run:           # nothing will change, so nothing to undo
            return
        try:
            group = self._undo_group_path()
            entries = self._read_manifest(group)
            relative = self.rel(path)
            if any(entry.get("path") == relative for entry in entries):
                return             # already recorded earlier in this turn
            index = len(entries) + 1
            if path.is_dir():
                backup = group / f"{index:03d}-dir"
                shutil.copytree(path, backup, symlinks=True)
                entry = {"path": relative, "kind": "dir", "backup": backup.name}
            elif path.exists():
                backup = group / f"{index:03d}-file"
                shutil.copy2(path, backup)
                entry = {"path": relative, "kind": "file", "backup": backup.name}
            else:
                entry = {"path": relative, "kind": "none", "backup": ""}
            entries.append(entry)
            (group / "manifest.json").write_text(
                json.dumps({"version": 1, "entries": entries}, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            # A failed snapshot must never stop the edit the user approved;
            # it only means this turn cannot be undone.
            if not self._undo_warned:
                self._undo_warned = True
                print(
                    notice("warn", f"could not record an undo snapshot: {exc} "
                                   "(the change will not be reversible)",
                           stream=sys.stderr),
                    file=sys.stderr,
                )

    def undo_groups(self) -> list[dict[str, Any]]:
        """Recorded groups, oldest first (by sequence, not by name)."""
        found: list[dict[str, Any]] = []
        for group in sorted(self._all_group_dirs(),
                            key=lambda path: (self._group_seq(path), path.name)):
            entries = self._read_manifest(group)
            if entries:
                found.append({"name": group.name, "path": group,
                              "seq": self._group_seq(group),
                              "entries": len(entries)})
        return found

    def undo_last(self) -> dict[str, Any]:
        """Put back the files the last request changed.

        Entries are restored in reverse order, so a turn that deleted a
        directory and then recreated one of its files ends up consistent.
        The group is only removed once every entry restored cleanly - a partial
        failure leaves the snapshots on disk to try again.
        """
        if self.dry_run:
            raise ValueError("dry run is on, so nothing has been changed to undo")
        groups = self.undo_groups()
        if not groups:
            raise ValueError(
                "nothing to undo - no file changes have been recorded in this "
                f"workspace ({self.rel(self.undo_root())}/)"
            )
        group = groups[-1]["path"]
        entries = self._read_manifest(group)

        restored: list[str] = []
        problems: list[str] = []
        for entry in reversed(entries):
            relative = str(entry.get("path") or "")
            kind = str(entry.get("kind") or "")
            try:
                target = self.resolve_write(relative)
                if kind == "none":
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                    restored.append(f"removed {relative}")
                elif kind == "dir":
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                    shutil.copytree(group / str(entry.get("backup")), target,
                                    symlinks=True)
                    restored.append(f"restored {relative}/")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(group / str(entry.get("backup")), target)
                    restored.append(f"restored {relative}")
            except (OSError, ToolError) as exc:
                problems.append(f"{relative}: {exc}")

        if problems:
            raise ValueError(
                "undo only partly worked, so the snapshots were kept:\n  "
                + "\n  ".join(problems)
                + ("\nrestored so far: " + ", ".join(restored) if restored else "")
            )

        try:
            shutil.rmtree(group)
        except OSError:
            pass
        return {
            "group": group.name,
            "restored": restored,
            "remaining": len(groups) - 1,
        }


# --------------------------------------------------------------------------- #
# Self-test: exercises the tool layer without any network access
# --------------------------------------------------------------------------- #

def run_selftest() -> int:
    """Offline checks so the harness can be verified without an API key."""
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn: collections.abc.Callable[[], None]) -> None:
        try:
            fn()
        except AssertionError as exc:
            results.append((name, False, str(exc) or "assertion failed"))
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
        else:
            results.append((name, True, ""))

    def expect_error(fn: collections.abc.Callable[[], Any], needle: str = "") -> None:
        try:
            fn()
        except ToolError as exc:
            assert needle.lower() in str(exc).lower(), (
                f"expected error containing {needle!r}, got {str(exc)!r}"
            )
        else:
            raise AssertionError(f"expected ToolError containing {needle!r}")

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)

        def make_harness(**kwargs: Any) -> Harness:
            # tester=False is explicit: the coder checks below write throwaway
            # files, so they must not inherit --tester from the command line.
            # The tester fence has its own checks, with tester=True.
            options: dict[str, Any] = {
                "api_key": "selftest-key",
                "auto_approve": True,
                "tester": False,
            }
            options.update(kwargs)
            return Harness(workspace, **options)

        harness = make_harness()

        def _capture(fn: collections.abc.Callable[[], Any]) -> str:
            """Run `fn` and return everything it printed to stdout."""
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                fn()
            return buffer.getvalue()


        # -- path safety ----------------------------------------------------
        check(
            "resolve blocks ../ escapes",
            lambda: expect_error(lambda: harness.resolve("../../etc/passwd"), "outside the workspace"),
        )
        check(
            "resolve blocks absolute outside paths",
            lambda: expect_error(lambda: harness.resolve("/etc/passwd"), "outside the workspace"),
        )
        check(
            "resolve blocks empty filename",
            lambda: expect_error(lambda: harness.resolve("   "), "empty"),
        )
        def _resolve_ok() -> None:
            resolved = harness.rel(harness.resolve("a/b/c.txt"))
            assert resolved == "a/b/c.txt", resolved

        check("resolve allows nested workspace paths", _resolve_ok)

        # -- write / read ---------------------------------------------------
        def _write_and_read() -> None:
            out = harness.tool_write_file("notes/inner.txt", "alpha\nbeta\n")
            assert "Created" in out, out
            text = harness.tool_read_file("notes/inner.txt")
            assert "alpha" in text and "beta" in text, text
            assert not (workspace / "notes/inner.txt").is_dir()

        check("write_file creates parents and read_file reads back", _write_and_read)

        def _read_range() -> None:
            harness.tool_write_file("lines.txt", "".join(f"L{i}\n" for i in range(1, 21)))
            text = harness.tool_read_file("lines.txt", start_line=5, end_line=7)
            assert "lines 5-7 of 20" in text, text
            assert "L5" in text and "L7" in text and "L8" not in text, text
            assert "read them with start_line=8" in text, text

        check("read_file honours start_line/end_line and hints the rest", _read_range)

        check(
            "read_file rejects a start beyond EOF",
            lambda: expect_error(
                lambda: harness.tool_read_file("lines.txt", start_line=999), "beyond the end"
            ),
        )
        check(
            "read_file rejects missing files",
            lambda: expect_error(lambda: harness.tool_read_file("nope.txt"), "not found"),
        )

        # -- edit_file ------------------------------------------------------
        def _edit_unique() -> None:
            harness.tool_write_file("edit.txt", "one\ntwo\nthree\n")
            out = harness.tool_edit_file("edit.txt", "two", "TWO")
            assert "1 replacement" in out, out
            assert (workspace / "edit.txt").read_text() == "one\nTWO\nthree\n"

        check("edit_file replaces a unique match and reports a diff", _edit_unique)

        def _edit_missing() -> None:
            expect_error(
                lambda: harness.tool_edit_file("edit.txt", "absent text", "x"), "not found"
            )
            assert (workspace / "edit.txt").read_text() == "one\nTWO\nthree\n", (
                "file changed despite a failed edit"
            )

        check("edit_file fails cleanly when old_string is absent", _edit_missing)

        def _edit_ambiguous() -> None:
            harness.tool_write_file("dup.txt", "same\nsame\n")
            expect_error(
                lambda: harness.tool_edit_file("dup.txt", "same", "other"), "ambiguous"
            )
            assert (workspace / "dup.txt").read_text() == "same\nsame\n", "file changed"

        check("edit_file refuses an ambiguous match", _edit_ambiguous)

        def _edit_replace_all() -> None:
            out = harness.tool_edit_file("dup.txt", "same", "other", replace_all=True)
            assert "2 replacement" in out, out
            assert (workspace / "dup.txt").read_text() == "other\nother\n"

        check("edit_file replace_all rewrites every occurrence", _edit_replace_all)

        def _edit_crlf() -> None:
            target = workspace / "crlf.txt"
            target.write_bytes(b"first\r\nsecond\r\n")
            harness.tool_edit_file("crlf.txt", "second", "SECOND")
            raw = target.read_bytes()
            assert raw == b"first\r\nSECOND\r\n", f"CRLF endings not preserved: {raw!r}"

        check("edit_file preserves CRLF line endings", _edit_crlf)

        check(
            "edit_file rejects an empty old_string",
            lambda: expect_error(
                lambda: harness.tool_edit_file("edit.txt", "", "x"), "must not be empty"
            ),
        )

        # -- search_files ---------------------------------------------------
        def _search() -> None:
            harness.tool_write_file("src/a.py", "def needle():\n    pass\n")
            harness.tool_write_file("src/b.txt", "a needle here\n")
            both = harness.tool_search_files("needle")
            assert "2 match(es)" in both, both
            assert "src/a.py:1:" in both, both
            only_py = harness.tool_search_files("needle", glob="*.py")
            assert "1 match(es)" in only_py and "src/b.txt" not in only_py, only_py
            none = harness.tool_search_files("zzz_not_there")
            assert "(no matches)" in none, none

        check("search_files finds matches, filters by glob", _search)

        check(
            "search_files reports invalid regexes",
            lambda: expect_error(lambda: harness.tool_search_files("[unclosed"), "invalid regular expression"),
        )

        def _search_case() -> None:
            # Use a token no other test file contains, or the counts collide.
            harness.tool_write_file("case.txt", "KwOnlyMarker\n")
            assert "1 match(es)" in harness.tool_search_files(
                "KwOnlyMarker", case_sensitive=True, path="case.txt"
            )
            assert "1 match(es)" in harness.tool_search_files(
                "kwonlymarker", case_sensitive=False, path="case.txt"
            )
            assert "(no matches)" in harness.tool_search_files(
                "kwonlymarker", case_sensitive=True, path="case.txt"
            )

        check("search_files honours case sensitivity", _search_case)

        # -- list_files -----------------------------------------------------
        def _list_ignores_noise() -> None:
            harness.tool_write_file("proj/main.py", "print(1)\n")
            harness.tool_write_file("proj/node_modules/dep/index.js", "x\n")
            listing = harness.tool_list_files("proj", recursive=True)
            assert "main.py" in listing, listing
            assert "node_modules" not in listing, f"noise directory leaked: {listing}"

        check("list_files skips noise directories when recursive", _list_ignores_noise)

        # -- delete_file ----------------------------------------------------
        def _delete_guards() -> None:
            harness.tool_write_file("del/keep.txt", "x")
            expect_error(
                lambda: harness.tool_delete_file("del"),
                "non-empty directory",
            )
            assert (workspace / "del/keep.txt").exists(), "directory removed despite the guard"
            harness.tool_delete_file("del", recursive=True)
            assert not (workspace / "del").exists(), "recursive delete did not remove the tree"
            expect_error(lambda: harness.tool_delete_file("."), "workspace root")

        check("delete_file guards non-empty dirs and the workspace root", _delete_guards)

        # -- the tester fence ------------------------------------------------
        # In tester mode the only writable path is the testing report, so the
        # write tools have to refuse everything else. These checks build their
        # own harness with tester=True; the ones above stay in coder mode.
        def _report_only_writes() -> None:
            tester = make_harness(tester=True)
            for attempt in (
                lambda: tester.tool_write_file("app.py", "print(1)\n"),
                lambda: tester.tool_edit_file("app.py", "a", "b"),
                lambda: tester.tool_append_file("notes.md", "x"),
            ):
                try:
                    attempt()
                except ToolError as exc:
                    assert "only file it may write" in str(exc), str(exc)
                else:
                    raise AssertionError("a source write was allowed in tester mode")
            assert not (workspace / "app.py").exists(), "a refused write still created a file"

            # dispatch() turns the refusal into an "ERROR:" result, so it has to
            # be checked as text rather than by catching.
            if "only file it may write" not in tester.dispatch(
                "write_file", {"filename": "src/app.py", "content": "x"}
            ):
                raise AssertionError("dispatch allowed a source write in tester mode")

            expect_error(
                lambda: tester.tool_delete_file(REPORT_PATH.as_posix()),
                "nothing is deleted",
            )

            wrote = tester.dispatch(
                "write_file",
                {"filename": REPORT_PATH.as_posix(), "content": "# handover\n"},
            )
            assert "Created" in wrote, wrote
            assert tester.report_path.read_text() == "# handover\n", wrote

            appended = tester.dispatch(
                "append_file",
                {"filename": REPORT_PATH.as_posix(), "content": "entry\n"},
            )
            assert "Appended" in appended, appended
            assert tester.report_path.read_text() == "# handover\nentry\n"

            # A crash-named delete in a resumed conversation still gets the
            # tester answer, not "unknown tool".
            assert "nothing is deleted" in tester.dispatch(
                "delete_file", {"filename": "app.py"}
            )

        check("tester mode writes only .harness_sessions/Testing.md", _report_only_writes)

        def _tester_tool_menu() -> None:
            coder_names = [tool["function"]["name"] for tool in tools_for(False)]
            tester_names = [tool["function"]["name"] for tool in tools_for(True)]
            assert coder_names == [tool["function"]["name"] for tool in TOOLS]
            assert "delete_file" in coder_names, coder_names
            assert "delete_file" not in tester_names, tester_names
            assert len(tester_names) == len(coder_names) - 1, tester_names
            for expected in ("list_files", "search_files", "read_file",
                             "write_file", "append_file", "execute_command"):
                assert expected in tester_names, tester_names
            # The coder descriptions must survive the copy untouched.
            coder_write = next(t for t in tools_for(False)
                               if t["function"]["name"] == "write_file")
            assert "Create a new file" in coder_write["function"]["description"]
            tester_write = next(t for t in tools_for(True)
                                if t["function"]["name"] == "write_file")
            assert "testing report" in tester_write["function"]["description"]

        check("the tester tool menu drops delete_file and rewrites the descriptions",
              _tester_tool_menu)

        def _tester_prompt_and_harness() -> None:
            tester = make_harness(tester=True)
            coder = make_harness(tester=False)
            assert "software tester" in tester.system_prompt(), tester.system_prompt()[:200]
            assert "coding assistant" in coder.system_prompt(), coder.system_prompt()[:200]
            assert "coding assistant" not in tester.system_prompt()
            assert tester.code_read_only and not coder.code_read_only
            assert tester.tester and not coder.tester
            assert "U · T E S T E R" in banner(None, tester=True) or \
                   "U - T E S T E R" in banner(None, tester=True)
            assert "U · H A R N E S S" in banner(None) or \
                   "U - H A R N E S S" in banner(None)
            assert "U-TESTER" not in banner(None)
            approve = dict(commands_for(True))
            assert "report writes" in approve["/approve [on|off]"], approve
            assert "write/edit/delete/execute" in dict(commands_for(False))["/approve [on|off]"]

        check("--tester switches the prompt, the logo and the harness fence",
              _tester_prompt_and_harness)

        def _tester_switch_is_live() -> None:
            # The UI turns tester mode on and off from its menu, so the switch
            # has to move the tools and the system turn, not just the flag.
            def names(h: Harness) -> list[str]:
                return [tool["function"]["name"] for tool in h.tools]

            switched = make_harness(tester=False)
            assert "delete_file" in names(switched), names(switched)
            _capture(lambda: switched.set_tester(True))
            assert switched.tester and switched.code_read_only
            assert "delete_file" not in names(switched), names(switched)
            assert "software tester" in switched.messages[0]["content"]
            expect_error(
                lambda: switched.tool_write_file("app.py", "print(1)\n"),
                "only file it may write",
            )
            _capture(lambda: switched.set_tester(False))
            assert not switched.tester and not switched.code_read_only
            assert "delete_file" in names(switched), names(switched)
            assert "coding assistant" in switched.messages[0]["content"]
            switched.tool_write_file("app.py", "print(1)\n")

        check("set_tester switches the tool menu and the system turn both ways",
              _tester_switch_is_live)

        def _tester_flag_is_seen_by_help() -> None:
            assert tester_requested(["--tester", "go"])
            assert tester_requested(["--test", "go"])       # argparse abbreviation
            assert not tester_requested(["go", "--yes"])
            assert not tester_requested([])
            assert "--tester" in build_parser(True).format_help()
            assert "U-TESTER" in build_parser(True).format_help()
            assert "U-HARNESS" in build_parser(False).format_help()

        check("--tester is parsed and changes the help text", _tester_flag_is_seen_by_help)

        # -- dry run --------------------------------------------------------
        def _dry_run() -> None:
            dry = make_harness(dry_run=True)
            out = dry.tool_write_file("dry.txt", "content")
            assert "DRY RUN" in out, out
            assert not (workspace / "dry.txt").exists(), "dry run wrote a file"
            out = dry.tool_execute_command("echo should-not-run")
            assert "DRY RUN" in out, out
            # The file must already exist, so create it for real first.
            harness.tool_write_file("dry_source.txt", "keep me\n")
            edited = dry.tool_edit_file("dry_source.txt", "keep me", "changed")
            assert "DRY RUN" in edited, edited
            assert (workspace / "dry_source.txt").read_text() == "keep me\n", (
                "dry run modified a file"
            )
            deleted = dry.tool_delete_file("dry_source.txt")
            assert "DRY RUN" in deleted, deleted
            assert (workspace / "dry_source.txt").exists(), "dry run deleted a file"

        check("dry_run performs no writes or commands", _dry_run)

        # -- command execution ---------------------------------------------
        def _command_output() -> None:
            out = harness.tool_execute_command(
                "printf 'a\\nb\\n'; echo oops >&2; exit 3"
            )
            assert "exit_code: 3" in out, out
            assert "a" in out and "b" in out, out
            assert "oops" in out, out

        check("execute_command captures stdout, stderr and exit code", _command_output)

        def _command_timeout() -> None:
            out = harness.tool_execute_command("sleep 30", timeout=1)
            assert "timed out after 1s" in out, out
            assert "hint:" in out, out

        check("execute_command kills a command that exceeds its timeout", _command_timeout)

        def _command_dangerous() -> None:
            blocked = harness.dispatch("execute_command", {"command": "rm -rf / --no-preserve-root"})
            assert blocked.startswith("ERROR: blocked"), blocked
            allowed = make_harness(allow_dangerous=True, dry_run=True)
            result = allowed.dispatch("execute_command", {"command": "shutdown now"})
            assert "DRY RUN" in result, result

        check("dangerous commands are blocked unless --allow-dangerous", _command_dangerous)

        def _command_chained_timeout() -> None:
            # A shell that spawns a child must die as a whole, otherwise the
            # harness would leak processes on every timeout.
            out = harness.tool_execute_command("sh -c 'sleep 30 & wait'", timeout=1)
            assert "timed out" in out, out

        check("timeout kills child processes too", _command_chained_timeout)

        def _kill_tree_falls_back() -> None:
            # The POSIX branch uses killpg; the fallback must also work when
            # that is unavailable (Windows) or the group is already gone.
            sleeper = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=(os.name == "posix"),
            )
            inst = make_harness()
            original = os.killpg if os.name == "posix" else None
            if original is not None:
                def boom(*_a, **_k):
                    raise OSError("no process group")
                os.killpg = boom  # type: ignore[attr-defined]
            try:
                inst._kill_tree(sleeper)
            finally:
                if original is not None:
                    os.killpg = original  # type: ignore[attr-defined]
            assert sleeper.poll() is not None, (
                "the plain-kill fallback left the process running"
            )

        check("_kill_tree still kills when killpg is unavailable",
              _kill_tree_falls_back)

        # -- approval -------------------------------------------------------
        def _denied_without_yes() -> None:
            strict = make_harness(auto_approve=False)
            result = strict.dispatch("write_file", {"filename": "denied.txt", "content": "x"})
            assert result.startswith("ERROR:") and "denied" in result, result
            assert not (workspace / "denied.txt").exists(), "wrote despite denial"

        if sys.stdin.isatty():
            results.append(("approval denial blocks writes", True, "skipped (stdin is a TTY)"))
        else:
            check("approval denial blocks writes", _denied_without_yes)

        def _approval_toggle() -> None:
            # /approve flips this attribute; _approve must honour it per call.
            local = make_harness(auto_approve=False)
            assert local._approve("Edit file", "denied.txt") is False
            local.auto_approve = True
            assert local._approve("Edit file", "denied.txt") is True
            result = local.dispatch(
                "write_file", {"filename": "toggled.txt", "content": "x"}
            )
            assert not result.startswith("ERROR:"), result
            assert (workspace / "toggled.txt").read_text() == "x"
            local.auto_approve = False
            assert local._approve("Edit file", "denied.txt") is False

        def _dangerous_stays_blocked() -> None:
            local = make_harness(auto_approve=True, allow_dangerous=False)
            assert local._approve("Run command", "rm -rf /", dangerous=True) is False
            local.allow_dangerous = True
            assert local._approve("Run command", "rm -rf /", dangerous=True) is True

        if sys.stdin.isatty():
            results.append(("approval toggle switches modes", True, "skipped (stdin is a TTY)"))
        else:
            check("approval toggle switches modes", _approval_toggle)
        check("dangerous commands stay gated", _dangerous_stays_blocked)

        # -- clipping and buffers ------------------------------------------
        def _clip_behaviour() -> None:
            text = "HEAD" + ("x" * 1000) + "TAIL"
            both = clip(text, 100)
            assert "truncated" in both and both.startswith("HEAD") and both.endswith("TAIL"), both
            assert len(clip(text, 100, keep="head")) < 200
            assert clip("short", 100) == "short"

        check("clip keeps head and tail and marks the cut", _clip_behaviour)

        def _bounded_buffer() -> None:
            buf = BoundedBuffer(50)
            for i in range(100):
                buf.add(f"line{i}\n")
            assert buf.dropped > 0, "buffer never dropped anything"
            assert len(buf.value()) < 200, "buffer grew past its limit"
            assert "line99" in buf.value(), "buffer kept the wrong end"

        check("BoundedBuffer keeps recent output and reports drops", _bounded_buffer)

        # -- assistant block wrapping --------------------------------------
        def _assistant_block_wraps() -> None:
            """Every row of the block keeps its left rail, however long the text.

            A long paragraph used to be left to the terminal, which wraps the
            row without the "│ " prefix: the rail vanished for the rest of the
            paragraph. The block must now fold its own lines.
            """
            probe = "intro\n" + ("alpha beta gamma " * 12) + (
                "https://example.com/" + "a" * 120
            ) + "\n\n  indented " + "x" * 120 + "\n" + (" " * 40) + "y" * 90
            saved = os.environ.get("COLUMNS")
            os.environ["COLUMNS"] = "60"
            try:
                rendered = assistant_block(probe)
            finally:
                if saved is None:
                    os.environ.pop("COLUMNS", None)
                else:
                    os.environ["COLUMNS"] = saved

            rows = rendered.splitlines()[1:]        # skip the "● assistant" heading
            assert len(rows) > 4, rendered
            bar = rows[0][len(RESULT_INDENT)]       # "│", or "|" on a dumb stream
            for row in rows:
                assert row.startswith(RESULT_INDENT + bar), row
                assert display_width(row) <= 60, (display_width(row), row)
            # Folding must not lose or duplicate a single character. Words can
            # be split (a 140-character URL has to break somewhere), so compare
            # with the whitespace taken out.
            folded = "".join(
                row[len(RESULT_INDENT) + 2:] if len(row) > len(RESULT_INDENT) + 2 else ""
                for row in rows
            )
            assert folded.replace(" ", "") == "".join(probe.split()), folded
            # The indented line stays indented on its continuation rows.
            tail = " ".join(
                row[len(RESULT_INDENT) + 2:] for row in rows
                if row[len(RESULT_INDENT) + 2:].strip()
            )
            assert "indented" in tail and "  indented" in tail, tail
            # A narrow window folds harder but keeps the rail too.
            os.environ["COLUMNS"] = "20"
            try:
                narrow = assistant_block("the quick brown fox jumps over the lazy dog")
            finally:
                if saved is None:
                    os.environ.pop("COLUMNS", None)
                else:
                    os.environ["COLUMNS"] = saved
            for row in narrow.splitlines()[1:]:
                assert row.startswith(RESULT_INDENT + bar), row
                assert display_width(row) <= 44, (display_width(row), row)
            # Short text is untouched: no gratuitous newlines in one-liners.
            assert assistant_block("done").splitlines()[1] == f"{RESULT_INDENT}{bar} done"

        check("assistant_block folds long lines and keeps the left rail",
              _assistant_block_wraps)

        # -- the final answer has its own colour ----------------------------
        def _answer_colour() -> None:
            """Only the answering block is painted in ANSWER_COLOUR.

            Mid-turn prose and the heading stay green; the block that ends the
            turn - the answer - is light blue, and the left rail keeps its
            grey so the block still has an outline.
            """
            keep = {name: os.environ.get(name) for name in
                    ("CLICOLOR_FORCE", "NO_COLOR", "TERM")}
            os.environ.pop("NO_COLOR", None)
            os.environ["CLICOLOR_FORCE"] = "1"
            os.environ["TERM"] = "xterm"
            try:
                answer = assistant_block("all done", final=True)
                prose = assistant_block("looking at the file", final=False)
            finally:
                for name, value in keep.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

            light = f"\x1b[{_STYLES[ANSWER_COLOUR]}m"
            green = f"\x1b[{_STYLES['green']}m"
            assert light in answer, repr(answer)
            assert green not in answer, repr(answer)
            assert green in prose, repr(prose)
            assert light not in prose, repr(prose)
            # Colour is decoration: the words are exactly what was asked for.
            bare = strip_ansi(answer).splitlines()
            assert bare[0].strip().endswith("assistant"), bare[0]
            assert bare[1] == f"{RESULT_INDENT}{glyphs()['v']} all done", bare[1]

        check("the final answer is drawn in ANSWER_COLOUR",
              _answer_colour)

        # -- one wrap width for every block ---------------------------------
        def _wrap_width() -> None:
            """Prose, notices and results all stop at OUTPUT_WIDTH columns.

            The wrap width is a setting, not a guess at the window size: a
            200-column terminal still gets OUTPUT_WIDTH columns, a narrow one
            only lowers it, and not one row of any block may overshoot it.
            """
            saved = os.environ.get("COLUMNS")
            try:
                os.environ["COLUMNS"] = "200"
                assert term_width() == OUTPUT_WIDTH, (term_width(), OUTPUT_WIDTH)

                prose = assistant_block("word " * 120)
                for row in prose.splitlines():
                    assert display_width(row) <= OUTPUT_WIDTH, (display_width(row), row)

                # A long notice wraps under itself, lined up with its text.
                note = notice("warn", "warning " * 40)
                rows = note.splitlines()
                assert len(rows) > 1, note
                for row in rows:
                    assert display_width(row) <= OUTPUT_WIDTH, (display_width(row), row)
                pad = " " * rows[0].index("warning")
                for row in rows[1:]:
                    assert row.startswith(pad), row
                    assert row.strip(), row

                # A long result line wraps instead of being cut with "…":
                # nothing is lost and every row keeps the left rail.
                body = "Q" * (OUTPUT_WIDTH * 2)
                block = result_block(
                    f"exit_code: 0\nstdout:\n{body}", summary_only=False
                )
                bare = strip_ansi(block)
                assert glyphs()["ellipsis"] not in bare, bare
                assert bare.count("Q") == len(body), bare.count("Q")
                for row in bare.splitlines():
                    assert display_width(row) <= OUTPUT_WIDTH, (display_width(row), row)
                rows_with_body = [row for row in bare.splitlines() if "QQQ" in row]
                assert len(rows_with_body) > 1, bare
                rail = glyphs()["v"]
                for row in rows_with_body:            # the rail on every fold
                    assert row.startswith(RESULT_INDENT + rail + " "), row

                # A narrow window still wins: never fold wider than the screen.
                os.environ["COLUMNS"] = "50"
                assert term_width() <= 50, term_width()
            finally:
                if saved is None:
                    os.environ.pop("COLUMNS", None)
                else:
                    os.environ["COLUMNS"] = saved
            # The setting comes from the environment; a nonsense value falls
            # back to the default instead of crashing the import.
            os.environ["HARNESS_SELFTEST_WIDTH"] = "wat"
            try:
                assert _env_int("HARNESS_SELFTEST_WIDTH", 80) == 80
                assert _env_int("HARNESS_SELFTEST_WIDTH_MISSING", 80) == 80
            finally:
                os.environ.pop("HARNESS_SELFTEST_WIDTH", None)
            os.environ["HARNESS_SELFTEST_WIDTH"] = "120"
            try:
                assert _env_int("HARNESS_SELFTEST_WIDTH", 80) == 120
            finally:
                os.environ.pop("HARNESS_SELFTEST_WIDTH", None)

        check("every block wraps at OUTPUT_WIDTH", _wrap_width)

        # -- a long command is wrapped, never cut ---------------------------
        def _command_lines() -> None:
            """The tool-call row and the allow? prompt show the whole command.

            Both used to put a shell command on one line and cut it with an
            ellipsis, so the thing being approved was the thing hidden. Now the
            command folds under its label and every row fits the width.
            """
            saved = os.environ.get("COLUMNS")
            try:
                os.environ["COLUMNS"] = "200"
                assert term_width() == OUTPUT_WIDTH, term_width()

                # A command that needs a few rows, sized for the width in
                # force: this check means the same thing at 40 columns as at
                # 80, and stays under ARG_MAX_ROWS so nothing is cut.
                per_row = max(16, term_width() - len("command") - 10)
                command = "python3 build.py" + " --flag=value" * max(
                    2, (per_row * 3) // len(" --flag=value")
                )
                block = strip_ansi(
                    tool_call_block("execute_command",
                                    json.dumps({"command": command}))
                )
                rows = block.splitlines()
                assert len(rows) > 2, block
                assert glyphs()["ellipsis"] not in block, block
                # Nothing lost: the command survives folding in one piece.
                squashed = "".join(block.split())
                assert command.replace(" ", "") in squashed, squashed
                lead = " " * (6 + len("command") + 2)   # the value column
                assert rows[1].lstrip().startswith('command  "'), rows[1]
                for row in rows[2:]:                    # continuation rows
                    assert display_width(row) <= OUTPUT_WIDTH, row
                    assert row.startswith(lead), row
                for row in rows[1:]:
                    assert display_width(row) <= OUTPUT_WIDTH, row

                # A pathologically long argument is bounded and marked.
                capped = strip_ansi(
                    tool_call_block("execute_command",
                                    json.dumps({"command": "x" * 4000}))
                ).splitlines()
                assert len(capped) == 1 + ARG_MAX_ROWS, capped
                assert capped[-1].rstrip().endswith(glyphs()["ellipsis"]), capped[-1]
                for row in capped[1:]:
                    assert display_width(row) <= OUTPUT_WIDTH, row

                # The allow? prompt wraps the command under the action name.
                prompt = strip_ansi(approval_prompt("Run command", command))
                prompt_rows = prompt.splitlines()
                assert len(prompt_rows) > 1, prompt
                assert prompt_rows[0].startswith("  ? Run command  "), prompt_rows[0]
                assert "Run command" in prompt_rows[0], prompt_rows[0]
                pad = " " * display_width("  ? Run command  ")
                for row in prompt_rows[1:]:
                    assert row.startswith(pad), row
                    assert row.strip(), row
                for row in prompt_rows:
                    assert display_width(row) <= OUTPUT_WIDTH, row
                assert command.replace(" ", "") in "".join(prompt.split()), prompt

                # The "run" line echoes the command it is about to run, and
                # still folds when it does not fit on one row.
                echo = notice("run", command, force=True, style="bold")
                if supports_color():
                    # Bold is applied per folded row, never to half a code.
                    assert echo.count("\x1b[1m") == len(echo.splitlines()), echo
                for row in strip_ansi(echo).splitlines():
                    assert display_width(row) <= OUTPUT_WIDTH, row
                echo_rows = strip_ansi(echo).splitlines()
                assert len(echo_rows) > 1, echo
                pad = " " * echo_rows[0].index(command.split()[0])
                for row in echo_rows[1:]:
                    assert row.startswith(pad), row

                # The run line the harness actually prints is a filled band:
                # every folded row carries the band background and is padded
                # to the transcript width, so a long command reads as one bar
                # across the screen instead of a ragged stripe.
                class _Tty:                     # a stream that claims colour
                    encoding = "utf-8"

                    def isatty(self) -> bool:
                        return True

                tty = _Tty()
                # The shade follows the terminal: the 256-colour ramp where it
                # exists, the old 16-colour light grey where it does not, and
                # black text either way.
                band_key = band_background(tty)
                if supports_256_colors(tty):
                    assert band_key == "bg_grey", band_key
                    assert _STYLES["bg_grey"] == f"48;5;{BAND_GREY}"
                else:
                    assert band_key == "bg_light_grey", band_key
                band_code = f"\x1b[{_STYLES[band_key]}m"
                band_rows = notice("run", command, force=True, stream=tty,
                                   background=band_key).splitlines()
                assert len(band_rows) > 1, band_rows
                if supports_color(tty):
                    for row in band_rows:                 # band on every row
                        assert band_code in row, row
                        assert "\x1b[30m" in row, row     # black text on it
                    assert "\x1b[1m" in band_rows[0], band_rows[0]
                    # The rail and label sit on the band too, not beside it.
                    assert strip_ansi(band_rows[0]).startswith(
                        "  " + glyphs(tty)["arrow"] + " run  "
                    ), band_rows[0]
                    for row in band_rows:                 # a bar, not a stripe
                        assert display_width(row) == term_width(), row
                else:                       # NO_COLOR or TERM=dumb wins
                    assert "\x1b[" not in band_rows[0], band_rows[0]
                    for row in band_rows:   # no band to draw, no padding
                        assert display_width(row) <= term_width(), row
                        assert not row.endswith(" "), row

                # A short command stays on one line - no wrap for its own sake.
                short_rows = strip_ansi(
                    tool_call_block("execute_command",
                                    json.dumps({"command": "ls -la"}))
                ).splitlines()
                assert len(short_rows) == 2, short_rows
                assert short_rows[1].endswith('"ls -la"'), short_rows[1]

                # A multi-line content argument is still summarised by size.
                summary = describe_value("one\ntwo\nthree")
                assert summary.startswith("3 lines, 13 chars"), summary
            finally:
                if saved is None:
                    os.environ.pop("COLUMNS", None)
                else:
                    os.environ["COLUMNS"] = saved

        check("long commands wrap instead of being cut", _command_lines)

        # -- line input ------------------------------------------------------
        def _editing_keys() -> None:
            # Backspace (both codes) must delete, not append a character.
            assert apply_editing_keys("helo\x7fp") == "help", apply_editing_keys("helo\x7fp")
            assert apply_editing_keys("abc\x08\x08") == "a", apply_editing_keys("abc\x08\x08")
            # A stray backspace on an empty line is harmless.
            assert apply_editing_keys("\x7f\x7fok") == "ok"
            # Arrow keys arrive as escape sequences; they must vanish.
            assert apply_editing_keys("ab\x1b[Dc\x1b[A") == "abc"
            assert apply_editing_keys("\x1b[3~x") == "x", apply_editing_keys("\x1b[3~x")
            # Control characters never survive.
            assert sanitise_line("a\x00b\x07c\x1b") == "abc", sanitise_line("a\x00b\x07c\x1b")
            assert "\x7f" not in sanitise_line("hi\x7f")

        check("apply_editing_keys honours backspace and drops escape sequences", _editing_keys)

        def _width() -> None:
            assert display_width("abc") == 3
            assert display_width(paint("abc", "bold", "red")) == 3, display_width(
                paint("abc", "bold", "red")
            )
            assert display_width("日本") == 4, display_width("日本")

        check("display_width ignores colour codes and counts wide characters", _width)

        def _wrapped_input() -> None:
            """A line longer than the terminal must wrap, not duplicate."""

            class Screen(io.StringIO):
                """A tiny terminal: keeps rows so a stale redraw shows up."""

                def __init__(self, columns: int) -> None:
                    super().__init__()
                    self.columns = columns
                    self.rows: list[list[str]] = [[]]
                    self.row = self.col = 0

                def _blank(self) -> None:
                    while len(self.rows) <= self.row:
                        self.rows.append([])

                def _put(self, char: str) -> None:
                    self._blank()
                    line = self.rows[self.row]
                    line.extend(" " * (self.col - len(line)))
                    if self.col < len(line):
                        line[self.col] = char
                    else:
                        line.append(char)
                    self.col += 1
                    if self.col >= self.columns:        # wrap, like a terminal
                        self.row, self.col = self.row + 1, 0

                def write(self, text: str) -> int:
                    index = 0
                    while index < len(text):
                        char = text[index]
                        match = re.match(r"\x1b\[([0-9]*)([A-Za-z])", text[index:])
                        if match:
                            count = int(match.group(1) or 1)
                            final = match.group(2)
                            if final == "A":
                                self.row = max(0, self.row - count)
                            elif final == "B":
                                self.row += count
                                self._blank()
                            elif final == "C":
                                self.col = min(self.columns - 1, self.col + count)
                            elif final == "D":
                                self.col = max(0, self.col - count)
                            elif final == "K":
                                self._blank()
                                del self.rows[self.row][self.col:]
                            elif final == "J":
                                self._blank()
                                del self.rows[self.row][self.col:]
                                del self.rows[self.row + 1:]
                            else:
                                raise AssertionError(f"unsupported escape: {final!r}")
                            index += match.end()
                            continue
                        if char == "\r":
                            self.col = 0
                        elif char == "\n":
                            self.row, self.col = self.row + 1, 0
                        else:
                            self._put(char)
                        index += 1
                    return len(text)

                def screen(self) -> str:
                    return "\n".join("".join(line) for line in self.rows)

            class Keys:
                def __init__(self, keys: list[str]) -> None:
                    self.keys = list(keys)

                def next(self) -> str:
                    return self.keys.pop(0) if self.keys else "ENTER"

            def run(columns: int, keys: list[str]) -> tuple[str, str]:
                screen = Screen(columns)
                saved = os.environ.get("COLUMNS")
                os.environ["COLUMNS"] = str(columns)
                try:
                    with contextlib.redirect_stdout(screen):
                        typed_back = _run_editor("you> ", [], Keys(keys))
                finally:
                    if saved is None:
                        os.environ.pop("COLUMNS", None)
                    else:
                        os.environ["COLUMNS"] = saved
                return typed_back, screen.screen()

            def only_once(rendered: str, expected: str) -> None:
                assert rendered.count("you>") == 1, rendered
                assert rendered.replace(" ", "").replace("\n", "") == "you>" + expected, rendered

            typed = "abcdefghijklmnopqrstuvwxyz"
            for columns in (7, 12, 40):          # 26 chars: several wraps each
                typed_back, rendered = run(columns, list(typed) + ["ENTER"])
                assert typed_back == typed, typed_back
                only_once(rendered, typed)

            # A line that ends exactly on the right margin (the wrap that
            # terminals disagree about) must render cleanly too.
            edge = "abcdefghijk"                 # 5 + 11 = 16 = 2 x 8
            typed_back, rendered = run(8, list(edge) + ["ENTER"])
            assert typed_back == edge, typed_back
            only_once(rendered, edge)

            # Editing in the middle of a wrapped line redraws both rows.
            typed_back, rendered = run(12, list(typed) + ["LEFT", "LEFT", "BACKSPACE", "ENTER"])
            assert typed_back == typed[:23] + typed[24:], typed_back
            only_once(rendered, typed_back)

        check("the line editor redraws wrapped lines without duplicating them",
              _wrapped_input)

        # -- history compaction --------------------------------------------
        def _compaction() -> None:
            compact = make_harness()
            compact.messages.append({"role": "user", "content": "the task"})
            for i in range(KEEP_RECENT_TOOL_RESULTS + 6):
                compact.messages.append({
                    "role": "tool",
                    "tool_call_id": f"c{i}",
                    "content": "z" * (MAX_HISTORY_CHARS // 4),
                })
            compact._compress_history()
            stubs = [m for m in compact.messages if str(m["content"]).startswith("[dropped]")]
            assert stubs, "nothing was compacted"
            assert compact.messages[1]["content"] == "the task", "user turn was modified"
            recent = compact.messages[-1]["content"]
            assert recent.startswith("z"), "the most recent tool output was dropped"

        check("_compress_history stubs old output and keeps the tail", _compaction)

        # -- /context -------------------------------------------------------
        def _context_meter() -> None:
            assert message_chars({"role": "user", "content": "abcd"}) == 4
            assert message_chars({"role": "assistant", "content": None}) == 0
            # Tool-call arguments are part of the request, so they must count.
            with_args = {
                "role": "assistant",
                "content": "ab",
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": '{"x": "yy"}'},
                }],
            }
            assert message_chars(with_args) == 2 + len('{"x": "yy"}'), message_chars(with_args)
            assert estimate_tokens(0, 0) == 0
            assert estimate_tokens(4) == 1
            assert estimate_tokens(5) == 2, "partial tokens must round up"
            assert estimate_tokens(4, 2) > estimate_tokens(4), "messages add overhead"

        check("message_chars and estimate_tokens measure the context", _context_meter)

        def _context_command() -> None:
            meter = make_harness()
            meter.messages = [
                {"role": "system", "content": "s" * 40},
                {"role": "user", "content": "u" * 40},
                {"role": "tool", "tool_call_id": "c1", "content": "t" * 40},
            ]
            meter.usage = {"prompt_tokens": 1234}
            printed = _capture(lambda: show_context(meter))
            assert "context" in printed, printed
            assert "120 chars" in printed, printed          # 3 x 40
            assert "3" in printed, printed                  # message count
            assert "1,234" in printed, printed              # API-reported tokens
            assert "estimated" in printed, printed

        check("/context reports role sizes, totals and API usage", _context_command)

        def _context_handles_empty_and_unbounded() -> None:
            empty = make_harness()
            empty.messages = []
            printed = _capture(lambda: show_context(empty))
            assert "0 chars" in printed, printed
            # Compaction off must not divide by zero.
            global MAX_HISTORY_CHARS
            saved = MAX_HISTORY_CHARS
            try:
                MAX_HISTORY_CHARS = 0
                printed = _capture(lambda: show_context(empty))
                assert "no limit" in printed, printed
            finally:
                MAX_HISTORY_CHARS = saved

        check("/context survives an empty conversation and disabled compaction",
              _context_handles_empty_and_unbounded)

        def _context_on_the_model_line() -> None:
            badge = make_harness()
            badge.model = "some-model:free"
            assert context_summary([]) == "empty", context_summary([])
            assert format_tokens(940) == "940", format_tokens(940)
            assert format_tokens(3_400) == "3.4k", format_tokens(3_400)
            assert format_tokens(12_345) == "12.3k", format_tokens(12_345)
            assert format_tokens(100_000) == "100k", format_tokens(100_000)

            # No messages at all says so instead of printing "0/100k".
            badge.messages = []
            line = strip_ansi(tier_badge(badge, stream=io.StringIO()))
            assert "some-model:free" in line, line
            assert "• context empty" in line or "- context empty" in line, line

            # A fresh harness already carries the system prompt, and the badge
            # counts it - it is part of every request.
            badge.messages = [{"role": "user", "content": "x" * 40}]
            line = strip_ansi(tier_badge(badge, stream=io.StringIO()))
            assert "empty" not in line, line
            if term_width() >= 40:      # room for the shortest form
                assert "14/100k" in line, line

            # The figure is the same one /context reports, next to the model.
            badge.messages = [{"role": "user", "content": "x" * 40_000}]
            expected = context_summary(badge.messages)
            assert expected == "10.0k/100k tokens (10%)", expected
            line = strip_ansi(tier_badge(badge, stream=io.StringIO()))
            assert "some-model:free" in line, line
            # Narrow terminals lose the wording first, then the figure itself,
            # so the badge never wraps onto a second line.
            bullet = glyphs(io.StringIO())["bullet"]
            head = f"  {bullet} free some-model:free"
            ladder = (
                (f"{head}  {bullet} context {expected}", f"context {expected}"),
                (f"{head}  {bullet} context 10.0k/100k (10%)",
                 "context 10.0k/100k (10%)"),
                (f"{head}  {bullet} 10.0k/100k (10%)", "10.0k/100k (10%)"),
            )
            for whole, tail in ladder:
                if display_width(whole) <= term_width():
                    assert tail in line, line
                    break
            else:
                assert line.strip().endswith("some-model:free"), line

            # Compaction off means no denominator to divide by.
            global MAX_HISTORY_CHARS
            saved = MAX_HISTORY_CHARS
            try:
                MAX_HISTORY_CHARS = 0
                assert context_summary(badge.messages) == "10.0k tokens", \
                    context_summary(badge.messages)
            finally:
                MAX_HISTORY_CHARS = saved

            # A model name too long for the terminal drops the figure rather
            # than wrapping the badge onto a second line.
            badge.model = "m" * 120
            line = strip_ansi(tier_badge(badge, stream=io.StringIO()))
            assert "context" not in line, line
            assert line.strip().endswith("m" * 120), line[-30:]

        check("the model line carries the context size", _context_on_the_model_line)

        # -- saved conversations --------------------------------------------
        def _session_roundtrip() -> None:
            keeper = make_harness()
            shutil.rmtree(keeper.sessions_dir, ignore_errors=True)
            keeper.messages.append({"role": "user", "content": "remember the number 41"})
            keeper.messages.append({"role": "assistant", "content": "noted"})
            path = keeper.save_session("my chat")
            assert path.exists(), "session file was not written"
            assert path.parent == keeper.sessions_dir, path
            assert path.stem == "my-chat", path.stem

            fresh = make_harness()
            item = fresh.load_session("my-chat")
            assert item["restored"] == 2, item
            assert fresh.messages[0]["role"] == "system", "system prompt was replaced"
            assert fresh.messages[-1]["content"] == "noted", fresh.messages
            assert "41" in fresh.messages[1]["content"], fresh.messages

            by_number = make_harness().load_session(" 1 ")
            assert by_number["name"] == "my-chat", by_number
            listed = make_harness().list_sessions()
            assert len(listed) == 1 and "41" in listed[0]["preview"], listed

        check("save_session/load_session round-trips a conversation", _session_roundtrip)

        def _session_names_stay_inside() -> None:
            tricky = make_harness()
            for name in ("../../etc/passwd", "/abs/path", "a/../../b"):
                path = tricky.save_session(name)
                assert path.parent == tricky.sessions_dir, path
                assert "/" not in path.name and ".." not in path.name, path.name
            for blank in ("..", "/", "///", "..."):
                try:
                    tricky._session_path(blank)
                except ValueError:
                    continue
                raise AssertionError(f"name {blank!r} should have been rejected")

        check("session names cannot escape the sessions directory", _session_names_stay_inside)

        def _session_errors() -> None:
            lonely = make_harness()
            shutil.rmtree(lonely.sessions_dir, ignore_errors=True)
            for bad in ("", "1", "nope"):
                try:
                    lonely.load_session(bad)
                except ValueError:
                    continue
                raise AssertionError(f"loading {bad!r} should have raised")
            stamped = lonely.save_session("   ")
            assert stamped.exists() and stamped.stem.startswith("chat-"), stamped

        check("load_session reports missing conversations", _session_errors)

        def _autosave_skips_empty() -> None:
            idle = make_harness()
            shutil.rmtree(idle.sessions_dir, ignore_errors=True)
            assert idle.autosave_session() is None, "an empty chat should not be saved"
            assert not idle.sessions_dir.exists(), "sessions dir was created needlessly"
            idle.autosave = False
            idle.messages.append({"role": "user", "content": "hi"})
            assert _capture(lambda: autosave_on_exit(idle)) == "", "quiet exit printed"
            assert not list(idle.sessions_dir.glob("*.json")), "--no-autosave still saved"

        check("autosave skips an empty conversation and honours autosave=False",
              _autosave_skips_empty)

        def _autosave_names() -> None:
            with tempfile.TemporaryDirectory() as room:
                home = Path(room)
                talker = Harness(home, api_key="selftest-key", auto_approve=True)
                talker.messages.append({"role": "user", "content": "plan the trip"})
                talker.messages.append({"role": "assistant", "content": "sure"})
                fresh = talker.autosave_session()
                assert fresh is not None and fresh.exists(), fresh
                assert fresh.stem.startswith("chat-"), fresh.stem
                printed = _capture(lambda: autosave_on_exit(talker))
                assert "/open" in printed, printed
                # Autosaving again with no /save or /open in between keeps
                # writing the same file: one chat is one file, however many
                # turns it takes.
                again = talker.autosave_session()
                assert again == fresh, (again, fresh)
                assert len(list(talker.sessions_dir.glob("*.json"))) == 1, (
                    "autosave piled up copies of one chat"
                )

                saved = talker.save_session("roadtrip")
                assert talker.autosave_session() == saved, (
                    "autosave ignored the saved name"
                )
                payload = json.loads(saved.read_text(encoding="utf-8"))
                assert [m["content"] for m in payload["messages"][1:]] == [
                    "plan the trip", "sure",
                ], payload
                # /save forked a named copy; the autosave file is still there.
                assert fresh.exists(), "the earlier autosave file vanished"
                assert len(list(talker.sessions_dir.glob("*.json"))) == 2, (
                    "expected the chat file plus roadtrip.json"
                )

                resumed = Harness(home, api_key="selftest-key", auto_approve=True)
                resumed.load_session("roadtrip")
                assert resumed.autosave_session() == saved, (
                    "autosave did not update the opened file"
                )
                assert len(list(resumed.sessions_dir.glob("*.json"))) == 2, (
                    "autosave made a copy"
                )

        check("autosave writes a new chat file, or updates the one in use",
              _autosave_names)

        # -- agent loop (fake client, no network) ---------------------------
        def fake_call(call_id: str, name: str, arguments: dict[str, Any]) -> Any:
            return types.SimpleNamespace(
                id=call_id,
                type="function",
                function=types.SimpleNamespace(
                    name=name, arguments=json.dumps(arguments)
                ),
            )

        def fake_response(content: str | None = None, tool_calls=()) -> Any:
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(
                        content=content, tool_calls=list(tool_calls)
                    )
                )],
                usage=None,
            )

        def fake_client(script: list[Any], sent: list[dict[str, Any]]) -> Any:
            """Stand-in for OpenAI(); records the kwargs of every call."""
            def create(**kwargs: Any) -> Any:
                sent.append(kwargs)
                return script.pop(0)

            return types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=create)
                )
            )

        def _loop_executes_tools() -> None:
            loop = make_harness()
            sent: list[dict[str, Any]] = []
            loop.client = fake_client(
                [
                    fake_response(tool_calls=[
                        fake_call("c1", "write_file",
                                  {"filename": "loop.txt", "content": "hi\n"}),
                        fake_call("c2", "read_file", {"filename": "loop.txt"}),
                    ]),
                    fake_response(content="Wrote and verified loop.txt"),
                ],
                sent,
            )
            result = loop.run("make loop.txt")
            assert result == "Wrote and verified loop.txt", result
            assert (workspace / "loop.txt").read_text() == "hi\n", "tool did not run"
            # Both tool results must be echoed back to the model.
            tool_msgs = [m for m in loop.messages if m["role"] == "tool"]
            assert len(tool_msgs) == 2, f"expected 2 tool results, got {len(tool_msgs)}"
            assert "Created loop.txt" in tool_msgs[0]["content"], tool_msgs[0]["content"]
            assert "hi" in tool_msgs[1]["content"], tool_msgs[1]["content"]
            assert tool_msgs[0]["tool_call_id"] == "c1", tool_msgs[0]
            # Tools are offered while the model still needs them.
            assert sent[0].get("tools"), "first call did not advertise tools"
            assert loop.api_calls == 2, loop.api_calls

        check("run() dispatches tool calls and returns the final answer", _loop_executes_tools)

        def _answer_marker_only_for_the_answer() -> None:
            """begin_answer() fires once per turn, for the answering block.

            The desktop pane cannot read ANSI colour, so it learns which block
            is the answer from this marker. Commentary printed beside a tool
            call must not raise it, or the whole transcript turns blue.
            """
            class Pane(io.StringIO):
                encoding = "utf-8"
                marks = 0

                def isatty(self) -> bool:
                    return False

                def answer_start(self) -> None:
                    self.marks += 1

            pane = Pane()
            talker = make_harness()
            sent: list[dict[str, Any]] = []
            talker.client = fake_client(
                [
                    fake_response(content="let me look",
                                  tool_calls=[fake_call("c1", "list_files", {})]),
                    fake_response(content="the answer"),
                ],
                sent,
            )
            with contextlib.redirect_stdout(pane):
                result = talker.run("check it")
            assert result == "the answer", result
            assert pane.marks == 1, f"expected one answer marker, got {pane.marks}"
            blocks = pane.getvalue().count(glyphs()["dot"] + " assistant")
            assert blocks == 2, (blocks, pane.getvalue())

        check("the answer marker fires once, on the answering block",
              _answer_marker_only_for_the_answer)

        def _loop_recovers_from_bad_json() -> None:
            bad = make_harness()
            sent: list[dict[str, Any]] = []
            bad.client = fake_client(
                [
                    fake_response(tool_calls=[
                        types.SimpleNamespace(
                            id="c1", type="function",
                            function=types.SimpleNamespace(
                                name="write_file", arguments="{not json"
                            ),
                        )
                    ]),
                    fake_response(content="recovered"),
                ],
                sent,
            )
            result = bad.run("try something invalid")
            assert result == "recovered", result
            tool_msgs = [m for m in bad.messages if m["role"] == "tool"]
            assert tool_msgs and "invalid JSON" in tool_msgs[0]["content"], tool_msgs

        check("run() survives malformed tool arguments", _loop_recovers_from_bad_json)

        def _loop_turn_limit() -> None:
            capped = make_harness(max_turns=2)
            sent: list[dict[str, Any]] = []
            looping = [fake_response(tool_calls=[
                fake_call(f"c{i}", "list_files", {})
            ]) for i in range(2)]
            capped.client = fake_client(
                looping + [fake_response(content="Here is where I got to.")], sent
            )
            result = capped.run("do a long task")
            assert result == "Here is where I got to.", result
            # The closing call must not offer tools, or the model would loop on.
            assert "tools" not in sent[-1], "the wrap-up call still advertised tools"

        check("run() asks for a summary when --max-turns is hit", _loop_turn_limit)

        def _run_autosaves_as_it_goes() -> None:
            # A run must not depend on the process living long enough to quit
            # tidily: each turn is on disk before the next API call goes out.
            with tempfile.TemporaryDirectory() as room:
                saver = Harness(Path(room), api_key="selftest-key",
                                auto_approve=True)
                on_disk: list[list[str]] = []
                script = [
                    fake_response(tool_calls=[
                        fake_call("c1", "write_file",
                                  {"filename": "note.txt", "content": "kept\n"}),
                    ]),
                    fake_response(content="wrote note.txt"),
                ]

                def create(**kwargs: Any) -> Any:
                    # Snapshot the sessions folder as each request goes out.
                    on_disk.append(
                        sorted(p.stem for p in saver.sessions_dir.glob("*.json"))
                    )
                    return script.pop(0)

                saver.client = types.SimpleNamespace(
                    chat=types.SimpleNamespace(
                        completions=types.SimpleNamespace(create=create)
                    )
                )
                assert saver.run("write note.txt") == "wrote note.txt"
                assert len(on_disk) == 2, on_disk
                assert all(names for names in on_disk), (
                    f"nothing saved before an API call: {on_disk}"
                )
                # The prompt survives the first request, the tool result the
                # second - and both land in one file, not a copy per turn.
                assert on_disk[0] == on_disk[1], f"a new file per turn: {on_disk}"
                (saved,) = saver.sessions_dir.glob("*.json")
                payload = json.loads(saved.read_text(encoding="utf-8"))
                blobs = [str(m.get("content") or "") for m in payload["messages"]]
                assert any("write note.txt" in blob for blob in blobs), payload
                assert any("note.txt" in blob for blob in blobs), payload

            # A crash in the middle of a turn still leaves the file behind.
            with tempfile.TemporaryDirectory() as room:
                boom = Harness(Path(room), api_key="selftest-key",
                               auto_approve=True)
                boom.client = fake_client(
                    [fake_response(tool_calls=[fake_call("c1", "list_files", {})])],
                    [],
                )

                def explode(*args: Any, **kwargs: Any) -> str:
                    raise RuntimeError("tool layer exploded")

                boom.dispatch = explode   # type: ignore[method-assign]
                try:
                    boom.run("book a flight")
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("the crash never reached the caller")
                crashed = list(boom.sessions_dir.glob("*.json"))
                assert crashed, "a crashed run saved nothing"
                payload = json.loads(crashed[0].read_text(encoding="utf-8"))
                blobs = [str(m.get("content") or "") for m in payload["messages"]]
                assert "book a flight" in blobs, payload

            # ...and --no-autosave really does keep every file unwritten.
            with tempfile.TemporaryDirectory() as room:
                quiet = Harness(Path(room), api_key="selftest-key",
                                auto_approve=True, autosave=False)
                quiet.client = fake_client([fake_response(content="hello")], [])
                assert quiet.run("say hello") == "hello"
                assert not list(quiet.sessions_dir.glob("*.json")), (
                    "autosave=False still wrote a conversation"
                )

        check("run() autosaves every turn, keeps one file, and survives a crash",
              _run_autosaves_as_it_goes)

        # -- skills ----------------------------------------------------------
        #
        # A skill only matters if the text actually reaches the model, so
        # these checks drive run() with a fake client and inspect the request.

        def _seed_skill() -> None:
            root = workspace / "skills"
            (root / "polite").mkdir(parents=True, exist_ok=True)
            (root / "polite" / "SKILL.md").write_text(
                "---\n"
                "name: polite\n"
                "description: say please\n"
                "---\n"
                "Always say please and thank you. POLITE_MARKER\n",
                encoding="utf-8",
            )

        def _skill_index() -> None:
            _seed_skill()
            book = make_harness().skills
            listed = book.discover()
            # The workspace skill is there. A checkout may also ship skills
            # beside uharness.py, so compare by name, not by the whole list.
            assert "polite" in [skill.name for skill in listed], listed
            index = book.index()
            assert "@{polite}" in index and "say please" in index, index
            # find() is case-insensitive over the name and the folder name...
            assert book.find("polite") is not None, "find() missed the name"
            assert book.find("POLITE") is not None, "find() should ignore case"
            # ...but it must never match description text from the frontmatter.
            assert book.find("say please") is None, "find() matched a description"

        check("skills are discovered, indexed and found by name", _skill_index)

        def _seed_checkout() -> Path:
            """A pretend checkout: a skills/ folder beside a fake uharness.py."""
            checkout = workspace / "checkout"
            for name, body in (
                ("hushed", "Whisper. HUSH_MARKER\n"),
                ("polite", "Bundled copy. BUNDLED_MARKER\n"),
            ):
                (checkout / "skills" / name).mkdir(parents=True, exist_ok=True)
                (checkout / "skills" / name / "SKILL.md").write_text(
                    f"---\nname: {name}\n---\n{body}", encoding="utf-8"
                )
            return checkout

        def _skills_next_to_the_script() -> None:
            # Whatever the working directory, the skills/ folder beside
            # uharness.py is searched too - and the workspace wins a name clash.
            global SCRIPT_DIR
            checkout = _seed_checkout()
            _seed_skill()
            saved, SCRIPT_DIR = SCRIPT_DIR, checkout
            try:
                book = SkillBook(workspace, None)
            finally:
                SCRIPT_DIR = saved
            assert book.roots[0] == (workspace / "skills").resolve(), book.roots
            assert book.roots[1] == (checkout / "skills").resolve(), book.roots
            names = [skill.name for skill in book.discover()]
            assert "hushed" in names, names
            # A project skill shadows a bundled one of the same name.
            polite = book.find("polite")
            assert polite is not None and "POLITE_MARKER" in polite.body, polite

        check("skills beside uharness.py are found, workspace first",
              _skills_next_to_the_script)

        def _skills_next_to_the_script_reach_the_model() -> None:
            # The system prompt index and @{name} loading both cover the
            # script-side folder, not just the workspace one.
            global SCRIPT_DIR
            checkout = _seed_checkout()
            saved, SCRIPT_DIR = SCRIPT_DIR, checkout
            try:
                stood_up = make_harness()
            finally:
                SCRIPT_DIR = saved
            assert "@{hushed}" in stood_up.system_prompt(), stood_up.system_prompt()
            loaded = stood_up.load_skills("@{hushed} please")
            assert [skill.name for skill in loaded] == ["hushed"], loaded
            assert "HUSH_MARKER" in stood_up.messages[-1]["content"]

        check("a skill beside uharness.py reaches the model",
              _skills_next_to_the_script_reach_the_model)

        def _skill_references() -> None:
            assert referenced_skills("hi @{polite} and @{polite}") == ["polite"]
            assert referenced_skills("no skills here") == []
            # An @{} with no name, or a bare @name, is not a reference.
            assert referenced_skills("email me @{} now") == []
            assert referenced_skills("email me @home") == []
            assert referenced_skills("a@{polite}") == [], "must not match mid-word"

        check("referenced_skills parses @{name} without false positives", _skill_references)

        def _skill_reaches_the_model() -> None:
            _seed_skill()
            trained = make_harness()
            sent: list[dict[str, Any]] = []
            trained.client = fake_client([fake_response(content="ok")], sent)
            trained.run("@{polite} greet the user")

            request = sent[0]["messages"]
            assert [m["role"] for m in request[:3]] == ["system", "user", "user"], request
            # The skill block must arrive *before* the prompt that asked for it.
            assert request[1]["content"].startswith(SKILL_BLOCK_PREFIX), request[1]
            assert "POLITE_MARKER" in request[1]["content"], "skill body was not sent"
            # The user's own words are untouched - no injected text in them.
            assert request[2]["content"] == "@{polite} greet the user", request[2]
            # And the index is in the system prompt so the model knows the skill.
            assert "@{polite}" in request[0]["content"], request[0]["content"]

        check("a referenced skill reaches the model before the prompt",
              _skill_reaches_the_model)

        def _skill_loaded_once() -> None:
            _seed_skill()
            chatter = make_harness()
            names = [skill.name for skill in chatter.load_skills("@{polite} one")]
            assert names == ["polite"], names
            users = sum(1 for m in chatter.messages if m["role"] == "user")
            # Referencing it again is a no-op: no re-send, no extra message.
            assert chatter.load_skills("@{polite} two") == [], "skill sent twice"
            assert sum(1 for m in chatter.messages if m["role"] == "user") == users, (
                "a repeat reference added a message"
            )

        check("a skill already in context is not sent again", _skill_loaded_once)

        def _skill_missing_is_warned() -> None:
            _seed_skill()
            lost = make_harness()
            lost.load_skills("@{nosuchskill} please")
            assert lost.messages[-1]["role"] == "user", lost.messages[-1]
            body = lost.messages[-1]["content"]
            assert "nosuchskill" in body and "polite" in body, body
            assert "without it" in body, body

        check("an unknown @{skill} is reported, not fatal", _skill_missing_is_warned)

        def _skills_absent_without_the_directory() -> None:
            # A workspace with no skills folder is not an error. The root is
            # pinned here, because the defaults also look beside uharness.py.
            empty = workspace / "no-skills"
            empty.mkdir(exist_ok=True)
            bare = Harness(
                empty, api_key="selftest-key", auto_approve=True, skills_dir="skills"
            )
            assert bare.skills.discover() == []
            assert bare.skills.index() == ""
            # No skill index is appended, so the prompt is the base rules only.
            assert "Available skills" not in bare.system_prompt()
            assert bare.load_skills("@{anything}") == [], "no skills dir must not raise"

        check("a workspace without skills/ works normally", _skills_absent_without_the_directory)

        # -- undo -----------------------------------------------------------
        # Every check gets its own workspace, so one undo cannot reach into
        # snapshots another check recorded.
        undo_root_dir = workspace / "undo-rooms"
        undo_root_dir.mkdir(exist_ok=True)

        def fresh_undo(room: str, **kwargs: Any) -> Harness:
            path = undo_root_dir / room
            path.mkdir(exist_ok=True)
            # tester=False: undo checks write throwaway files, so they must not
            # inherit --tester from the command line.
            options: dict[str, Any] = {
                "api_key": "selftest-key",
                "auto_approve": True,
                "tester": False,
            }
            options.update(kwargs)
            return Harness(path, **options)

        def _undo_removes_created_file() -> None:
            local = fresh_undo("created")
            assert local.undo_root() == local.sessions_dir / "undo", local.undo_root()
            local.tool_write_file("new.txt", "hello\n")
            assert (local.workspace / "new.txt").exists()
            result = local.undo_last()
            assert result["restored"] == ["removed new.txt"], result
            assert not (local.workspace / "new.txt").exists(), "undo left the new file"
            assert local.undo_groups() == [], local.undo_groups()

        check("undo deletes a file the agent created", _undo_removes_created_file)

        def _undo_restores_overwritten_file() -> None:
            local = fresh_undo("overwritten")
            local.tool_write_file("keep.txt", "original\n")
            local._undo_group = None       # this is what _run_turn does per request
            local.tool_write_file("keep.txt", "changed\n")
            local.tool_write_file("also.txt", "x\n")
            local.tool_edit_file("keep.txt", "changed", "changed again")
            assert (local.workspace / "keep.txt").read_text() == "changed again\n"
            result = local.undo_last()
            # Three tool calls in one turn are one group and one undo.
            assert len(result["restored"]) == 2, result
            assert (local.workspace / "keep.txt").read_text() == "original\n"
            assert not (local.workspace / "also.txt").exists()
            assert result["remaining"] == 1, result
            local.undo_last()
            assert not (local.workspace / "keep.txt").exists(), (
                "the earlier turn should have been undone too"
            )

        check("undo restores an overwritten file and reverts one turn at a time",
              _undo_restores_overwritten_file)

        def _undo_restores_deleted_tree() -> None:
            local = fresh_undo("tree")
            (local.workspace / "pkg").mkdir()
            (local.workspace / "pkg" / "a.txt").write_text("a\n")
            local._undo_group = None
            local.tool_delete_file("pkg", recursive=True)
            local.tool_write_file("pkg/a.txt", "recreated\n")
            local.undo_last()
            assert (local.workspace / "pkg" / "a.txt").read_text() == "a\n", (
                "reverse-order restore should put the original tree back"
            )

        check("undo restores a deleted tree, newest change first",
              _undo_restores_deleted_tree)

        def _undo_nothing_to_do() -> None:
            local = fresh_undo("empty")
            try:
                local.undo_last()
            except ValueError as exc:
                assert "nothing to undo" in str(exc), exc
            else:
                raise AssertionError("undo on an empty workspace should refuse")

        check("undo with no recorded changes explains itself", _undo_nothing_to_do)

        def _undo_dry_run_records_nothing() -> None:
            local = fresh_undo("dry", dry_run=True)
            local.tool_write_file("dry.txt", "y\n")
            assert local.undo_groups() == [], "a dry run recorded a snapshot"
            try:
                local.undo_last()
            except ValueError as exc:
                assert "dry run is on" in str(exc), exc
            else:
                raise AssertionError("undo should refuse while dry_run is on")

        check("dry_run records nothing and refuses to undo", _undo_dry_run_records_nothing)

        def _undo_denied_write_records_nothing() -> None:
            local = fresh_undo("denied", auto_approve=False)
            out = local.dispatch("write_file", {"filename": "no.txt", "content": "x"})
            assert out.startswith("ERROR:"), out
            assert local.undo_groups() == [], "a denied write left a snapshot behind"

        if sys.stdin.isatty():
            results.append(("approval denial records no snapshot", True,
                            "skipped (stdin is a TTY)"))
        else:
            check("approval denial records no snapshot",
                  _undo_denied_write_records_nothing)

        def _undo_prunes_old_groups() -> None:
            local = fresh_undo("prune")
            local.tool_write_file("f.txt", "0\n")
            for index in range(local.UNDO_KEEP + 3):
                local._undo_group = None
                local.tool_write_file("f.txt", f"{index}\n")
            groups = local.undo_groups()
            assert len(groups) == local.UNDO_KEEP, len(groups)
            assert len(local._all_group_dirs()) == local.UNDO_KEEP, "unpruned dirs on disk"
            seqs = [group["seq"] for group in groups]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
            local.undo_last()
            # The newest group holds the state from before the newest write.
            assert (local.workspace / "f.txt").read_text() == f"{local.UNDO_KEEP + 1}\n"

        check(f"only the newest {Harness.UNDO_KEEP} groups are kept",
              _undo_prunes_old_groups)

        def _undo_keeps_snapshots_on_failure() -> None:
            local = fresh_undo("partial")
            local.tool_write_file("p.txt", "old\n")
            local._undo_group = None
            local.tool_write_file("p.txt", "new\n")
            group = local.undo_groups()[-1]["path"]
            for backup in group.glob("*-file"):
                backup.unlink()          # a missing backup makes the restore fail
            try:
                local.undo_last()
            except ValueError as exc:
                assert "partly worked" in str(exc), exc
            else:
                raise AssertionError("a broken snapshot must not report success")
            assert group.is_dir(), "snapshots were dropped after a failed undo"
            assert (local.workspace / "p.txt").read_text() == "new\n", (
                "a failed undo must not touch the file"
            )

        check("a failed undo keeps the snapshots", _undo_keeps_snapshots_on_failure)

        def _undo_list_output() -> None:
            local = fresh_undo("listing")
            empty = _capture(lambda: show_undo(local, "list"))
            assert "nothing recorded" in empty, empty
            local.tool_write_file("x.txt", "1\n")
            listed = _capture(lambda: show_undo(local, "list"))
            assert "change(s)" in listed, listed
            done = _capture(lambda: show_undo(local))
            assert "undid" in done and "removed x.txt" in done, done
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                show_undo(local, "sideways")
            assert "usage" in err.getvalue(), err.getvalue()

        check("/undo list and /undo print the right thing", _undo_list_output)

    passed = sum(1 for _name, ok, _detail in results if ok)
    print(f"selftest: {passed}/{len(results)} checks passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL {name}: {detail}")
    if passed != len(results):
        return 1
    print("All tool-layer checks passed (no network used).")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def tester_requested(argv: list[str]) -> bool:
    """True if --tester (or an unambiguous abbreviation) is on the command line.

    --help has to describe the mode the user asked for, but argparse only knows
    about --tester after it has parsed. A throwaway parser answers the question
    first, using argparse's own abbreviation rules, and ignores everything it
    does not recognise.
    """
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--tester", action="store_true")
    try:
        known, _unknown = probe.parse_known_args(argv)
    except SystemExit:      # e.g. an ambiguous abbreviation: the real parser
        return False        # will report it properly
    return bool(known.tester)


def build_parser(tester: bool = False) -> argparse.ArgumentParser:
    """The CLI. `tester` only chooses which help text is printed."""
    if tester:
        description = (
            "Tester agent: reads a handover, checks the code, runs the tests\n"
            "and reports defects in .harness_sessions/Testing.md. It never\n"
            "changes the code under test.\n"
            "Run without --tester for the coder half of the same loop."
        )
        examples = (
            '  python3 uharness.py --tester --yes "test the handover in .harness_sessions/Testing.md"\n'
            '  python3 uharness.py --tester --yes "verify round 2 and report the defects"\n'
            '  python3 uharness.py --tester --dry-run "check src/importer.py and run its tests"\n'
            '  python3 uharness.py --tester --verbosity full "run the tests and show the output"\n'
            '  python3 uharness.py --tester "@{handover to tester} review the last changes"\n'
            '  python3 uharness.py --selftest     # verify tools offline\n'
            "  python3 uharness.py --tester       # interactive\n"
            "\n"
            "Tester rule: the only writable file is .harness_sessions/Testing.md.\n"
            "The write tools refuse every other path and delete_file is not\n"
            "offered, so a run can never change the code it is reviewing.\n"
        )
        approve_help = (
            "  /approve [on|off]   toggle asking before writing the report or\n"
            "                      running a command (or answer 'always' at an\n"
            "                      allow? prompt)\n"
        )
        yes_help = (
            "auto-approve report writes and command execution (does not lift "
            "the tester fence - only .harness_sessions/Testing.md is writable - "
            "and does not lift the dangerous-command blocklist, which needs "
            "--allow-dangerous)"
        )
        banner_help = "start quietly, without the U-TESTER text-art logo"
    else:
        description = (
            "Agent harness: let an LLM read, edit and run files for you.\n"
            "Free by default on DeepSeek's deepseek-v4.1-flash:free via Token\n"
            "Harbor; /model paid switches to DeepSeek's own endpoint.\n"
            "--tester runs the same tool layer as the tester half of the\n"
            "two-agent development loop."
        )
        examples = (
            '  python3 uharness.py "Create hello.py that prints hello"\n'
            '  python3 uharness.py --yes "Read data.csv and write report.md"\n'
            '  python3 uharness.py --dry-run "Rename things in utils.py"\n'
            '  python3 uharness.py --verbosity full "Explain how auth works"\n'
            '  python3 uharness.py "@{test} inventory the docs/ folder"\n'
            '  python3 uharness.py --selftest     # verify tools offline\n'
            "  python3 uharness.py                # interactive\n"
        )
        approve_help = (
            "  /approve [on|off]   toggle asking before write/edit/delete/execute\n"
            "                      (or answer 'always' at an allow? prompt)\n"
        )
        yes_help = (
            "auto-approve file writes, edits, deletes and command execution "
            "(does not lift the dangerous-command blocklist - that needs "
            "--allow-dangerous)"
        )
        banner_help = "start quietly, without the U-HARNESS text-art logo"
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            + examples
            + "\n"
            "Interactive commands (blank arguments are shown as [name];\n"
            "/help inside a session prints the same list):\n"
            "  /save [name]        save the conversation to .harness_sessions/\n"
            "  /open [name]        pick a saved conversation and continue it\n"
            "  /sessions           list saved conversations\n"
            "  /skills             list skill names\n"
            + approve_help +
            "  /model [free|paid]  show the model in use, or switch tier\n"
            "  /context            show how much context the conversation uses\n"
            "  /verbose [level]    quiet, brief or full - how much result text prints\n"
            "  /reset              forget the current conversation\n"
            "  /undo [list]        put back the files the last request changed;\n"
            "                      'list' shows what is recorded\n"
            "  /help               this list\n"
            "  /exit               leave (Ctrl+D and 'exit' work too)\n"
            "Aliases: /? for /help, /ls for /sessions, /skill for /skills,\n"
            "/auto for /approve, /ctx for /context, /verbosity for /verbose,\n"
            "/quit for /exit. A bare 'exit' or 'quit' leaves too. An unknown\n"
            "slash command only prints a warning - it never reaches the model.\n"
            "\n"
            "Skills: type @{name} anywhere in a prompt to load\n"
            "skills/<name>/SKILL.md (or skills/<name>.md) from <workspace>/skills\n"
            "and from the skills/ folder beside this script; /skills lists them,\n"
            "and HARNESS_SKILLS_DIR replaces both folders.\n"
            "\n"
            "API keys: TOKENHARBOR_API_KEY for the free tier (the default) or\n"
            "DEEPSEEK_API_KEY for the paid tier; /model switches at runtime.\n"
            "\n"
            "The conversation is autosaved after every turn and again on exit,\n"
            "so even a crash leaves /open something to resume from\n"
            "(--no-autosave turns that off).\n"
        ),
    )
    parser.add_argument("prompt", nargs="*", help="what you want the agent to do")
    parser.add_argument("-i", "--interactive", action="store_true", help="keep asking for prompts")
    parser.add_argument(
        "--tester", action="store_true",
        help=(
            "run as the tester half of the two-agent loop: read "
            f"{REPORT_PATH.as_posix()}, test the work it describes and report "
            "defects there. The write tools accept that one path only - the "
            "code under test is read-only - and delete_file is not offered"
        ),
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help=yes_help,
    )
    parser.add_argument(
        "--allow-dangerous", action="store_true",
        help="also permit commands matching the dangerous-command blocklist",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show what the agent would change without changing anything",
    )
    parser.add_argument(
        "--selftest", action="store_true",
        help="run offline checks of the tool layer and exit (no API key needed)",
    )
    parser.add_argument(
        "-w", "--workspace", default=os.getcwd(),
        help="directory the agent is allowed to work in (default: current directory)",
    )
    parser.add_argument(
        "-m", "--model", default=DEFAULT_MODEL,
        help=(
            f"model to use (default: {DEFAULT_MODEL}, the free Token Harbor "
            f"tier; /model paid switches to {PAID_MODEL} at "
            f"{PAID_BASE_URL})"
        ),
    )
    parser.add_argument(
        "--max-turns", type=int, default=20,
        help=(
            "maximum model/tool round trips per request (default: 20); the "
            "model is asked to summarise when it is reached"
        ),
    )
    parser.add_argument(
        "--command-timeout", type=int, default=120,
        help=(
            "default shell command timeout in seconds (default: 120, max "
            f"{MAX_COMMAND_TIMEOUT}); the model can still ask for a different "
            "one per call"
        ),
    )
    parser.add_argument(
        "--thinking", action="store_true",
        help="enable the model's thinking/reasoning mode",
    )
    parser.add_argument(
        "--usage", action="store_true",
        help="print token usage for each request",
    )
    parser.add_argument(
        "--verbosity", choices=list(VERBOSITY_LEVELS), default=VERBOSITY,
        help=(
            "how much of each tool result is echoed to the console: "
            "brief (headline + one result line, no command output; default), "
            "full (everything) or quiet (same as brief, without context/usage "
            "lines). "
            "The model and --log always receive the full result. "
            f"Env fallback: HARNESS_VERBOSITY (now: {VERBOSITY})"
        ),
    )
    parser.add_argument(
        "--no-autosave", action="store_true",
        help=(
            "do not autosave the conversation - by default every turn is "
            f"written to {SESSION_DIR_NAME}/ as it happens, and once more when "
            "the session ends (/exit, Ctrl+D and Ctrl+C all save), so /open "
            "can resume it; this flag removes both writes"
        ),
    )
    parser.add_argument(
        "--no-banner", action="store_true",
        help=banner_help,
    )
    parser.add_argument(
        "--log", default=None,
        help="append the conversation as JSON lines to this file",
    )
    return parser


COMMANDS: list[tuple[str, str]] = [
    ("/save [name]", "save this conversation (blank: timestamp name)"),
    ("/open [name]", "list saved conversations and continue one"),
    ("/sessions", "list saved conversations (alias /ls)"),
    ("/skills", "list skill names (alias /skill)"),
    ("/approve [on|off]", "ask before write/edit/delete/execute (alias /auto)"),
    ("/model [free|paid]", "show the model in use, or switch tier"),
    ("/context", "how much context the conversation uses (alias /ctx)"),
    ("/verbose [level]", "quiet, brief or full (alias /verbosity)"),
    ("/reset", "forget the current conversation"),
    ("/undo", "put back the files the last request changed (/undo list)"),
    ("/help", "this list (alias /?)"),
    ("/exit", "leave (alias /quit; 'exit' and Ctrl+D too)"),
]


def commands_for(tester: bool = False) -> list[tuple[str, str]]:
    """/help rows. Only the /approve line differs between the two modes."""
    if not tester:
        return COMMANDS
    return [
        (command, "ask before report writes and commands (alias /auto)"
         if command.startswith("/approve") else description)
        for command, description in COMMANDS
    ]


def show_commands(tester: bool = False) -> None:
    print()
    print(panel("commands", commands_for(tester)))
    print(paint("  unknown commands are warned about, never sent to the model",
                "dim"))


def show_skills(harness: Harness) -> None:
    """Print the skill names - one line when they fit, folded when they do not."""
    skills = harness.skills.discover()
    if not skills:
        # Name every folder that was searched, so the user knows where a skill
        # would be picked up from.
        roots = harness.skills.describe_roots()
        print(notice("info", f"no skills yet - add <name>/SKILL.md in {roots}",
                     force=True))
        return
    label = "skills: "
    pad = " " * display_width(label)
    rows = wrap(", ".join(skill.name for skill in skills),
                max(20, term_width() - display_width(label)))
    print(paint(label, "bold", "cyan") + rows[0])
    for row in rows[1:]:                 # a long list folds under the label
        print(pad + row)


def show_context(harness: Harness) -> None:
    """Report how big the conversation has grown, in characters and tokens.

    Characters are exact; tokens are an estimate because the harness never
    tokenises locally. Whatever the API last reported is shown separately, and
    it is usually larger - the request also carries the tool schemas.
    Typing /context is a direct request for this panel, so it prints in every
    verbosity, unlike the usage lines printed after each turn.
    """
    tally: dict[str, int] = {"system": 0, "user": 0, "assistant": 0, "tool": 0}
    for message in harness.messages:
        role = str(message.get("role") or "user")
        tally[role if role in tally else "user"] += message_chars(message)

    total = sum(tally.values())
    count = len(harness.messages)
    # Same helper the prompt's model line uses, so the badge and this panel
    # always agree.
    tokens = context_tokens(harness.messages)

    rows: list[tuple[str, str]] = [
        (role, f"{tally[role]:,} chars") for role in tally if tally[role]
    ]
    rows.append(("messages", f"{count:,}"))
    rows.append(("total", f"{total:,} chars"))
    rows.append(("tokens", f"~{tokens:,} (estimated)"))

    if MAX_HISTORY_CHARS > 0:
        share = 100 * total / MAX_HISTORY_CHARS
        left = MAX_HISTORY_CHARS - total
        if left > 0:
            where = f"{share:.0f}% of {MAX_HISTORY_CHARS:,} - {left:,} chars left"
        else:
            where = f"over {MAX_HISTORY_CHARS:,} - compaction is dropping tool output"
    else:
        where = "no limit (compaction disabled)"
    rows.append(("budget", where))

    reported = harness.usage.get("prompt_tokens")
    if reported:
        rows.append((
            "last sent",
            f"{reported:,} tokens (API count, includes tool schemas)",
        ))

    print()
    print(panel("context", rows))


def show_sessions(harness: Harness) -> list[dict[str, Any]]:
    """Print saved conversations; returns them so callers can index into it."""
    sessions = harness.list_sessions()
    if not sessions:
        print()
        print(notice(
            "info",
            f"no saved conversations yet - /save [name] writes one to "
            f"{harness.rel(harness.sessions_dir)}/",
        ))
        return sessions
    width = term_width()
    print()
    print(paint("  saved conversations", "bold", "cyan"))
    for index, item in enumerate(sessions, 1):
        print(
            paint(f"  {index:>2}. ", "bold", "cyan")
            + fit(item["name"], max(16, width - 42), stream=sys.stdout)
            + paint(f"   {item['messages']} messages   {item['saved_at']}", "dim")
        )
        preview = fit(item["preview"], max(20, width - 8))
        print("      " + paint(preview, "dim", "italic"))
    return sessions


def show_undo(harness: Harness, argument: str = "") -> None:
    """`/undo` puts the last request's changes back; `/undo list` shows them."""
    if argument == "list":
        groups = harness.undo_groups()
        if not groups:
            print(notice(
                "info",
                f"nothing recorded yet - changes are snapshotted to "
                f"{harness.rel(harness.undo_root())}/ as they happen",
                force=True,
            ))
            return
        rows = [
            (item["name"], f"{item['entries']} change(s)") for item in groups[-10:]
        ]
        print()
        print(panel("undo", rows))
        print(paint("  /undo puts the newest one back", "dim"))
        return
    if argument:
        print(notice("warn", "usage: /undo - or /undo list to see what is recorded",
                     stream=sys.stderr), file=sys.stderr)
        return
    try:
        result = harness.undo_last()
    except ValueError as exc:
        print(notice("error", str(exc), stream=sys.stderr), file=sys.stderr)
        return
    print(notice("ok", f"undid {result['group']}: restored "
                       f"{len(result['restored'])} path(s)"))
    for line in result["restored"]:
        for row in wrap("  " + line, term_width()):
            print(paint(row, "dim"))
    if result["remaining"]:
        print(paint(f"  {result['remaining']} earlier change(s) still undoable",
                    "dim"))
    else:
        print(paint("  nothing older to undo", "dim"))


def choose_session(harness: Harness) -> str | None:
    """Show the saved conversations and return what the user picked."""
    if not show_sessions(harness):
        return None
    try:
        answer = read_line(
            paint("\n  open conversation", "bold")
            + paint(" (number or name, blank to cancel) ", "dim")
            + paint(">", "bold", "cyan") + " "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return answer or None


def autosave_on_exit(harness: Harness) -> None:
    """Write the conversation on the way out so /open can pick it up again.

    Called from every way out of the interactive loop (Ctrl+C, Ctrl+D, /exit,
    "exit"), so a session is never lost just because quitting was the last
    thing typed. A failed save warns and lets the exit continue - losing a
    conversation is bad, but refusing to quit would be worse.
    """
    if not harness.autosave:
        return
    try:
        path = harness.autosave_session()
    except (OSError, ValueError) as exc:
        print(notice("warn", f"could not autosave: {exc}", stream=sys.stderr),
              file=sys.stderr)
        return
    if path is None:
        return
    print(notice(
        "info",
        f"autosaved {harness.rel(path)} - /open {path.stem} carries on from here",
    ))


def interactive_loop(harness: Harness) -> None:
    # Built from COMMANDS so the greeting cannot drift out of step with /help.
    listing = ", ".join(name.split(" ")[0] for name, _ in COMMANDS)
    print()
    print(notice("info", f"Commands: {listing}"))
    print(notice(
        "info",
        "/help explains them and gives the aliases. At an allow? prompt: "
        "y or n, or 'always' to stop asking for the rest of the session "
        "(Ctrl+C stop loop)",
    ))
    entered: list[str] = []          # up-arrow recalls what you typed
    while True:
        try:
            # The tier (and model) sits on its own line above the prompt, so
            # "which model am I on?" never needs a command. read_line prints
            # the head once and redraws only the inline "you> " part.
            line = read_line(
                tier_badge(harness) + "\nyou> ", history=entered,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            autosave_on_exit(harness)
            return
        if not line:
            continue
        entered.append(line)

        if line.startswith("/"):
            word, _, argument = line.partition(" ")
            word = word.lower()
            argument = argument.strip()

            if word in {"/exit", "/quit"}:
                autosave_on_exit(harness)
                return
            if word in {"/help", "/?"}:
                show_commands(harness.tester)
                continue
            if word in {"/approve", "/auto"}:
                if argument not in {"", "on", "off", "toggle"}:
                    print(
                        notice("warn", "usage: /approve [on|off] - blank toggles",
                               stream=sys.stderr),
                        file=sys.stderr,
                    )
                    continue
                if argument == "on":
                    enabled = True
                elif argument == "off":
                    enabled = False
                else:
                    enabled = not harness.auto_approve
                harness.set_auto_approve(enabled)
                continue
            if word == "/model":
                if argument == "":
                    # The same line that sits above the prompt, so the answer
                    # carries the context size too instead of a bare model name.
                    print("\n" + tier_badge(harness))
                elif argument in {"free", "paid"}:
                    try:
                        harness.set_free(argument == "free")
                    except ValueError as exc:
                        print(notice("error", f"cannot switch: {exc}",
                                     stream=sys.stderr),
                              file=sys.stderr)
                else:
                    print(
                        notice("warn", "usage: /model [free|paid] - blank shows the model",
                               stream=sys.stderr),
                        file=sys.stderr,
                    )
                continue
            if word == "/reset":
                harness.reset()
                print("Conversation cleared.")
                continue
            if word == "/undo":
                show_undo(harness, argument)
                continue
            if word in {"/sessions", "/ls"}:
                show_sessions(harness)
                continue
            if word in {"/context", "/ctx"}:
                show_context(harness)
                continue
            if word in {"/verbose", "/verbosity"}:
                if argument:
                    try:
                        harness.set_verbosity(argument)
                    except ValueError as exc:
                        print(notice("warn", str(exc), stream=sys.stderr),
                              file=sys.stderr)
                else:
                    print(notice(
                        "info",
                        f"verbosity {harness.verbosity} - "
                        "/verbose full|brief|quiet",
                        force=True,
                    ))
                continue
            if word in {"/skills", "/skill"}:
                show_skills(harness)
                continue
            if word == "/save":
                try:
                    path = harness.save_session(argument or None)
                except (OSError, ValueError) as exc:
                    print(notice("error", f"could not save: {exc}", stream=sys.stderr),
                          file=sys.stderr)
                    continue
                print(notice("ok", f"saved {harness.rel(path)}"))
                continue
            if word == "/open":
                selector = argument or choose_session(harness)
                if not selector:
                    continue
                try:
                    item = harness.load_session(selector)
                except ValueError as exc:
                    print(notice("error", str(exc), stream=sys.stderr), file=sys.stderr)
                    continue
                print(notice(
                    "ok",
                    f"opened {item['name']} - {item['restored']} messages, "
                    f"saved {item['saved_at']}",
                ))
                continue
            print(notice("warn", f"unknown command {word} - /help lists them",
                         stream=sys.stderr), file=sys.stderr)
            continue

        if line in {"exit", "quit"}:
            autosave_on_exit(harness)
            return
        try:
            harness.run(line)
        except KeyboardInterrupt:
            # Ctrl+C abandons the request, not the session.
            print()
            print(notice("warn", "interrupted - back at the prompt", stream=sys.stderr),
                  file=sys.stderr)


def main() -> int:
    global TESTER

    parser = build_parser(tester_requested(sys.argv[1:]))
    args = parser.parse_args()

    # Set the mode before anything else, so the selftest, the banner, the
    # system prompt and the tool menu all agree on which job this run is doing.
    TESTER = bool(args.tester)

    if args.selftest:
        return run_selftest()

    # Apply --verbosity (or the HARNESS_VERBOSITY fallback) before any output,
    # so the banner and the very first tool result already follow it.
    apply_verbosity(args.verbosity)

    if not (os.environ.get(FREE_API_KEY_ENV)
            or os.environ.get(PAID_API_KEY_ENV)):
        print(
            "Error: no API key is set.\n"
            f'  export {FREE_API_KEY_ENV}="thk_live_..."   (free tier)\n'
            f'  export {PAID_API_KEY_ENV}="sk-..."          (paid tier)\n'
            "  (or run --selftest, which needs no key)",
            file=sys.stderr,
        )
        return 2

    workspace = Path(args.workspace).expanduser()
    if not workspace.is_dir():
        for row in wrap(f"Error: workspace is not a directory: {workspace}",
                        term_width()):
            print(row, file=sys.stderr)
        return 2

    harness = Harness(
        workspace,
        model=args.model,
        auto_approve=args.yes,
        allow_dangerous=args.allow_dangerous,
        dry_run=args.dry_run,
        max_turns=max(1, args.max_turns),
        command_timeout=max(1, args.command_timeout),
        thinking=args.thinking,
        log_path=Path(args.log).expanduser() if args.log else None,
        show_usage=args.usage,
        autosave=not args.no_autosave,
        tester=args.tester,
    )

    rows: list[tuple[str, str]] = [
        ("model", harness.model),
        ("profile", "free" if harness.free else "paid"),
        ("workspace", str(harness.workspace)),
        ("tools", f"{len(harness.tools)} available"),
    ]
    if harness.tester:
        rows.append(("role", f"tester (writes only {REPORT_PATH.as_posix()})"))
    if args.dry_run:
        rows.append(("mode", "dry run (nothing will be changed)"))
    elif harness.tester:
        approval = ("auto (--yes)" if args.yes
                    else "ask before report writes and commands")
        rows.append(("approval", approval))
    else:
        approval = ("auto (--yes)" if args.yes
                    else "ask before write/edit/delete/execute")
        rows.append(("approval", approval))
    if harness.autosave:
        rows.append(("autosave", f"after every turn to {SESSION_DIR_NAME}/"))

    if args.no_banner:
        for label, value in rows:
            head = f"{label + ':':<10} "
            paired = wrap(value, max(20, term_width() - len(head)))
            for index, row in enumerate(paired):
                print((head if index == 0 else " " * len(head)) + row)
    else:
        print()
        print(banner(tester=harness.tester))
        print()
        print(panel("session", rows))
    print()

    prompt = " ".join(args.prompt).strip()
    if prompt:
        harness.run(prompt)
    if args.interactive or not prompt:
        interactive_loop(harness)
    return 0


if __name__ == "__main__":
    sys.exit(main())
