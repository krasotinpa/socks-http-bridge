# Contributing

Thanks for wanting to improve the Bridge! This guide is the short path for human
contributors. Agents working in this repository follow [AGENTS.md](AGENTS.md),
which documents the same rules in more detail.

## Ground rules

- **Repository artifacts are in English**: code, comments, commit messages,
  issues, pull requests, and docs.
- **The Bridge is a Windows application** — the tray, Autostart, and parts of
  the test suite use Windows APIs. Develop and run the checks on Windows.
- **Domain language**: read [GLOSSARY.md](GLOSSARY.md) before naming things.
  Bridge, Listener, Upstream, Tunnel, Config, and Apply have precise meanings;
  use the glossary's terms.
- **Past decisions**: the [ADR collection](docs/adr/) records why things are
  the way they are. If your change contradicts an ADR, say so in the PR instead
  of silently overriding it.

## Workflow

1. **Features start as an issue.** Open one in
   [GitHub Issues](https://github.com/krasotinpa/socks-http-bridge/issues) so
   scope is agreed before code is written. Small fixes (typos, obvious bugs)
   can go straight to a PR.
2. **Every change lives on its own feature branch.** `main` moves only through
   merged pull requests.
3. **Set up with [uv](https://docs.astral.sh/uv/)** (Python 3.13 or newer):

   ```
   uv sync
   ```

   `uv sync` creates `.venv` and installs the runtime and dev dependencies;
   `uv run <command>` executes inside that environment.
4. **Run the checks before opening or updating a PR**:

   ```
   uv run pytest
   uv run ruff check .
   uv run mypy src
   ```

   Put a summary of what ran and how it ended in the PR body.
5. **Open the PR against `main`.** Merging stays with the maintainer.

## Scope of a change

- The smallest change that fully solves the task: reuse what exists, no
  unrequested abstractions.
- Implement every behaviour the task asks for, completely, and nothing beyond
  it. A pre-existing bug or a performance concern the task doesn't mention is
  a follow-up to report in the PR, not something to fix in the same change.
- Where the task is ambiguous, implement the reading its wording and the
  surrounding code most directly support, and state the assumption in the PR.

## Tests

- Test external behaviour, not implementation details.
- Keep tests hermetic, like the existing suite: ephemeral ports (bind port 0),
  never touch a live Bridge or Upstream, tkinter roots withdrawn without a
  mainloop, process and shortcut seams mocked.
- Size tests like the neighbouring files: roughly one focused test per stated
  behaviour.
