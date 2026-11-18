# Pure Python tray app, no packaged exe

The app ships as source run by `pythonw.exe` (no console), installed by a script
that installs pip dependencies and manages the Startup shortcut. We deliberately
do not build a PyInstaller/Go executable: Defender already deleted a downloaded
Go proxy binary on this machine, and packaged proxy-themed executables raise the
same AV risk, while `pythonw.exe` + stdlib core is proven and silent.

## Consequences

The bridge core stays stdlib-only; tray/UI dependencies are limited to `pystray`,
`Pillow`, and stdlib `tkinter`. Installation requires the user's Python
(install-manager shims at `%LOCALAPPDATA%\Python\bin` are stable). Updates are
`git pull`, not re-downloading binaries.
