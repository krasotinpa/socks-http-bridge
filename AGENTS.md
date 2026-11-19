# Working in this repository

## Language

The conversation and the repository are in different languages, on purpose: the
conversation has one reader, the repository has many.

- **Answer the user in Russian** — chat replies, plans, work reports.
- **Write repository artifacts in English** — code, comments, commit messages, GitHub
  issues and PRs, `README.md`, `AGENTS.md`, `docs/`.

## Git workflow

- **Branches**: every change goes on its own feature branch; `main` moves only through
  merged pull requests.
- **Pull requests**: open them without asking. A PR is a review request, not a merge, so
  the branch rule already keeps `main` safe and waiting for confirmation only delays
  review. Merging stays with the maintainer, or with an agent the maintainer explicitly
  delegated to deliver the task end to end.
- Implementation follows the matt-pocock implement skill: tdd at the agreed seams,
  regular typecheck and single-file test runs, full suite at the end, then the
  code-review skill before opening the PR.
- **Checks**: this repository has no CI, so run the checks locally before opening or
  updating a PR and put a summary of what ran and how it ended in the PR body:

  ```
  uv run pytest
  uv run ruff check .
  uv run mypy src
  ```

  The project is uv-managed: `uv sync` creates `.venv` and installs the runtime and
  dev dependencies; `uv run` executes commands inside that environment. If uv
  cannot find the install-manager Python on its own, pass
  `--python %LOCALAPPDATA%\Python\bin\python.exe` to `uv sync`.

## Scope of a change

- Write code in ponytail mode (the `ponytail` skill): the smallest change that fully
  solves the task — reuse what exists, no unrequested abstractions.
- Implement every behaviour the task asks for, completely, and nothing beyond it. A
  pre-existing bug, a performance concern or behaviour the task doesn't mention is a
  follow-up to report in your summary, not something to fix in this change, unless the
  requested behaviour cannot work without it.
- Where the task is ambiguous, implement the reading its wording and the surrounding code
  most directly support, and state that assumption in your summary. When the ambiguity is
  about what the user wants rather than what the code says, ask instead of guessing.
- Commit tests where the task asks for them or the repository already keeps tests for that
  kind of change, sized like the neighbouring test files: roughly one focused test per
  stated behaviour. Scratch checks stay scratch.
- Edit files surgically; rewrite a file whole only when it is short or most of it changes.

## Where things are

- **Issues** live in GitHub Issues for `krasotinpa/socks-http-bridge`, via `gh`.
  See `docs/agents/issue-tracker.md`.
- **Labels**: five triage roles. See `docs/agents/triage-labels.md`.
- **Domain**: single context. Before grepping, read `GLOSSARY.md` (the glossary), then the
  ADRs in `docs/adr/` that touch your area. Name things with the glossary's terms; when
  your change contradicts an ADR, say so rather than silently overriding it.
  See `docs/agents/domain.md`.
- **Reference core**: `reference/bridge.py` is the proven prototype the application core
  is built from (CONNECT tunnels + plain-HTTP forwarding, SOCKS5 with remote DNS, no idle
  timeout on tunnels). Behaviour changes to the core need a test that pins the old
  behaviour first.

## Environment

- Development machine: Windows 11, git-bash shell with MSYS path conversion disabled —
  pass `C:/...` forward-slash paths to native tools (python, git, taskkill), never `/c/...`.
- Python: install-manager shims at `%LOCALAPPDATA%\Python\bin` (3.15);
  `pythonw.exe` alongside it runs the tray app without a console.
- Runtime dependencies: `pystray`, `Pillow` (tray icon), stdlib `tkinter` (settings window).
  The bridge core itself stays stdlib-only.
- Defender silently deletes downloaded Go proxy binaries — keep this project pure Python;
  after downloading any executable, verify the file still exists.
