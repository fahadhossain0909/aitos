# Linux AITOS Operator Console

Native Linux session manager for the authenticated read-only gateway. Not a trading
runner, daemon, VPS installer or public-facing service.

Requirements: Debian/Ubuntu desktop, Python 3.11+, Tk, Node 22.12+, dpkg-deb,
and an unlocked Secret Service provider (GNOME Keyring or compatible KWallet).

Build from repository root: `bash native/linux/build.sh`
Output: `artifacts/linux/aitos-console_<version>_<arch>.deb` and SHA256SUMS.
Install: `sudo apt install ./artifacts/linux/aitos-console_0.1.0_amd64.deb`
Run: `aitos-console`; uninstall: `sudo apt remove aitos-console`.

Credentials use Python keyring's Secret Service backend. If D-Bus/Secret Service
is unavailable, locked, or plaintext backend selected, startup fails closed.
No plaintext file/environment fallback. No root privileges at runtime. Launcher
asks for an existing backend port (including SSH-forwarded port) and bearer token.
It never starts trading or installs backend. VPS tunnel:
`ssh -N -L 18090:127.0.0.1:8090 user@host`; select local port 18090.

Targets Debian-family amd64 and arm64. AppImage, RPM, signed packages, updater,
embedded Wayland window, physical-machine acceptance and macOS packaging remain
future gates. Package is unsigned test software.
