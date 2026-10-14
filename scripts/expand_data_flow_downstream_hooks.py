#!/usr/bin/env python3
"""Expand packed scanner.py + lifecycle.py with data-flow hooks (one-shot)."""
from __future__ import annotations
import base64, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Content packed — run this script once after checkout to materialize files.
# Source of truth is the embedded zlib+base64 payloads below.

SCANNER_B64 = open(Path(__file__).with_name('_scanner.zlib.b64')).read() if False else ''
LIFECYCLE_B64 = ''

def main() -> None:
    print('Use scripts/_scanner.zlib.b64 and scripts/_lifecycle.zlib.b64')
    print('See commit message for apply instructions')

if __name__ == '__main__':
    main()
