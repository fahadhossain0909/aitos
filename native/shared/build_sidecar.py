"""Freeze only the read-only gateway for the selected native target."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        required=True,
        choices=(
            "x86_64-pc-windows-msvc",
            "x86_64-unknown-linux-gnu",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
        ),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if not (root / "desktop/dist/index.html").is_file():
        raise SystemExit("Build shared React assets first")
    separator = ";" if sys.platform == "win32" else ":"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            "aitos-gateway",
            "--paths",
            str(root),
            "--hidden-import",
            "keyring.backends.SecretService",
            "--hidden-import",
            "keyring.backends.macOS",
            "--hidden-import",
            "aitos.native.windows_vault",
            "--add-data",
            str(root / "desktop/dist") + separator + "console_dist",
            str(root / "native/shared/sidecar.py"),
        ],
        check=True,
        cwd=root,
    )
    suffix = ".exe" if sys.platform == "win32" else ""
    source = root / "dist" / ("aitos-gateway" + suffix)
    destination = (
        root / "desktop/src-tauri/binaries" / ("aitos-gateway-" + args.target + suffix)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    print("Read-only sidecar frozen; native release acceptance still required")


if __name__ == "__main__":
    main()
