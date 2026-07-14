---
name: install-project
description: Merge the Docker Claude toolkit's .claude skills and scripts into the current project without deleting folders or overwriting existing files. Use when the user asks to install, set up, or update this toolkit in a project.
argument-hint: "[--target <project-path>] [--dry-run]"
disable-model-invocation: true
---

# Install Docker Claude into a project

Install the bundled project files with the plugin's safe merge installer.

## Command

Run this command with Bash, passing arguments as separate argv values (never concatenate untrusted input into a shell string):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install-project.py" --target "${CLAUDE_PROJECT_DIR}"
```

Interpret `$ARGUMENTS` as follows:

- No arguments: install into `${CLAUDE_PROJECT_DIR}`.
- `--target <project-path>`: use that directory instead.
- `--dry-run`: preview the merge without writing.
- Both flags may be combined.
- Reject any other argument; do not invent or forward unsupported flags.

## Safety behavior

The installer only considers these bundled payloads:

- `.claude/skills/**` → `<target>/.claude/skills/**`
- operational files in `scripts/**` → `<target>/scripts/**`

It creates missing directories and copies missing files. Existing files are never overwritten:

- Same content: reported as unchanged.
- Different content at the same path: reported as a conflict and left untouched.
- Unrelated existing files: never inspected beyond their destination path and never removed.

After installation, report the copied, unchanged, and conflicting counts. If any project skills were newly added, tell the user to run `/reload-plugins` or restart Claude Code.
