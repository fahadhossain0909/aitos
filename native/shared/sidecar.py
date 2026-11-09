"""Frozen entrypoint, shared by Windows/Linux/macOS Tauri packages."""

from aitos.native.embedded_gateway import main

if __name__ == "__main__":
    raise SystemExit(main())
