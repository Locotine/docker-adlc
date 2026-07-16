---
name: install-project
description: Synchronize the Docker Claude toolkit's .claude skills and scripts into the current project. Plugin-managed files are overwritten by the installed plugin version; unrelated project files are preserved. Use when the user asks to install, set up, or update this toolkit in a project.
argument-hint: "[--target <project-path>] [--dry-run]"
disable-model-invocation: true
---

# Install Docker Claude into a project

Install or update the bundled project files with the plugin's synchronization installer.

## Command

Run this command with Bash, passing arguments as separate argv values (never concatenate untrusted input into a shell string):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install-project.py" --target "${CLAUDE_PROJECT_DIR}"
```

Interpret `$ARGUMENTS` as follows:

- No arguments: install into `${CLAUDE_PROJECT_DIR}`.
- `--target <project-path>`: use that directory instead.
- `--dry-run`: preview the synchronization without writing.
- Both flags may be combined.
- Reject any other argument; do not invent or forward unsupported flags.

## Safety behavior

The installer only considers these bundled payloads:

- `.claude/skills/**` → `<target>/.claude/skills/**`
- operational files in `scripts/**` → `<target>/scripts/**`

It creates missing directories and synchronizes every plugin-managed file:

- Same content: reported as unchanged.
- Different content at the same path: atomically overwritten with the bundled version.
- Unrelated existing files: never inspected beyond their destination path and never removed.
- Destination symlinks and non-regular files: rejected without following or replacing them.

After installation, report the copied, overwritten, and unchanged counts. Tell the user to run `/reload-plugins` or restart Claude Code so updated project skills are reloaded.
