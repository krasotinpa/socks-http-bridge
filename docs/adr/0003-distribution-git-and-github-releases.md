# Distribution: git install and GitHub Release artifacts, no PyPI, no exe

The Bridge is a Windows-only, pure-Python tray application installed per machine
from source (ADR-0002). Publishing to PyPI would add release machinery and a
permanent package name for a tool whose install path is `git clone` plus
`scripts/install.py`; a packaged exe would reintroduce the antivirus
false-positive risk that ADR-0002 rejected. We therefore distribute source only:
the repository is the install source, and each GitHub Release carries the sdist
and wheel built with `uv build` for users who prefer artifacts over a checkout.

## Consequences

- Version identity: the release tag matches the package version in
  `pyproject.toml` (semver); the About window shows the installed version from
  package metadata, the single source of truth.
- Updating means pulling the repository (or downloading a newer release
  artifact) and re-running the installer; there is no
  `pip install socks-http-bridge` from PyPI.
- Release creation is a manual, deliberate step (`uv build` + `gh release
  create`), not an automatic publish on every merge.
