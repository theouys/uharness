---
name: test
description: Inventory a folder - count its files, summarise what each one contains, and write a markdown report into that same folder.
---

# Test - Folder Inventory Skill

Use this skill when the user points you at a folder and wants to know what is in
it. It is written for `/home/theo/tmp/test` but works on any directory.

## What the user gets

Three things, in this order, every time:

1. **How many files** the folder holds.
2. **A summary of each file** - what it is for and what is actually inside it.
3. **A markdown report written into that same folder.**

## Procedure

1. **Confirm the folder is reachable.** The agent may only touch paths inside
   its workspace (`-w`, default: the directory you started it in). If the target
   folder sits outside the workspace, stop and say so - either restart with
   `-w <parent-of-target>` or ask the user - rather than trying to climb out with
   `../` paths. The tool layer will refuse those anyway.
2. **List it, then count.** Use `list_files` on the folder. Count **regular
   files**, recursively. Keep these categories separate in your tally:
   - **content files** - the real material (source, data, text, media);
   - **hidden/dotfiles** and sub-folders;
   - **generated noise** - `__pycache__`, `.git`, `*.pyc`, caches, the report you
     are about to write. Report these but never let them pad the headline number.
   An empty folder is a valid answer: report `0` files and say so plainly.
3. **Read every file** before describing it. Use `read_file` with
   `start_line`/`end_line` to page through anything long; never describe a file
   you have only guessed at from its name. For unreadable, binary or oversized
   files, say "not read - binary/too large" instead of inventing contents.
4. **Write the report** with `write_file`, into the target folder itself, as
   `report.md`. If `report.md` already exists, write `report-<YYYY-MM-DD>.md`
   instead - do not clobber someone's earlier report - and name the file you
   actually wrote in your reply.
5. **Report back in chat** as well: the count, the per-file summary, and the
   path of the report you wrote. The file does not replace the answer.

## Report shape

```markdown
# Folder inventory: /home/theo/tmp/test

- Generated: <YYYY-MM-DD>
- Files found: <n>
- Sub-folders: <n>

## Files

| # | File | Size | What it is |
|---|------|------|------------|
| 1 | `notes.txt` | 412 B | Three-line todo list; mentions a dentist appointment. |

## Notes

Anything surprising: empty files, duplicates, binaries, things that look
generated, anything worth a human's attention.
```

Keep the "What it is" column to one sentence per file - concrete, not vague.
"Config for the Retry class: 3 keys, timeout 30s" beats "a config file".

## Rules that outrank a tidy report

- **Never invent contents.** Unreadable means unreadable. Say it in both the
  table and the note.
- **Never write outside the target folder.** One report file, nothing else.
- **Never delete or reorganise** the user's files to make the inventory cleaner.
- An empty folder is a finding, not a failure. Do not fill the report with
  apology - one line is enough, then the empty table and the reason.
