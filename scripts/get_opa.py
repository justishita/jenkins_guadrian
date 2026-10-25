"""Download the pinned OPA binary into ``.bin/`` so the real-policy tests run.

**Owner:** P3. Without ``opa`` the tests that check fixes against the actual Rego -
TC-11, TC-15, and the generated TC-03/TC-08 fixes - skip silently, and a green run
would not prove the guardrails work. This fetches the same version the ``opa``
container runs, verifies it against the release's published SHA-256, and puts it
where ``conftest.py`` and ``scripts/demo_p3.py`` look for it.

    python scripts/get_opa.py

Idempotent: an existing binary of the right version is left alone.
"""

from __future__ import annotations

import hashlib
import platform
from pathlib import Path
import stat
import subprocess
import sys
import urllib.request


VERSION = "1.21.1"  # keep in step with the opa image in docker-compose.yml
REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_DIR = REPO_ROOT / ".bin"
BASE_URL = f"https://github.com/open-policy-agent/opa/releases/download/v{VERSION}"


def asset_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "opa_windows_amd64.exe"
    if system == "linux":
        return "opa_linux_arm64_static" if machine in {"aarch64", "arm64"} else "opa_linux_amd64_static"
    if system == "darwin":
        return "opa_darwin_arm64_static" if machine in {"arm64", "aarch64"} else "opa_darwin_amd64"
    raise SystemExit(f"no OPA build known for {system}/{machine}")


def binary_path() -> Path:
    return TARGET_DIR / ("opa.exe" if platform.system().lower() == "windows" else "opa")


def installed_version(path: Path) -> str | None:
    try:
        output = subprocess.run(
            [str(path), "version"], capture_output=True, text=True, timeout=30, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - pinned https URL
        return response.read()


def main() -> int:
    target = binary_path()
    if target.is_file() and installed_version(target) == VERSION:
        print(f"opa {VERSION} already at {target}")
        return 0

    asset = asset_name()
    print(f"downloading opa {VERSION} ({asset}) ...")
    binary = fetch(f"{BASE_URL}/{asset}")
    expected = fetch(f"{BASE_URL}/{asset}.sha256").decode("utf-8").split()[0].strip().lower()
    actual = hashlib.sha256(binary).hexdigest()
    if actual != expected:
        print(f"checksum mismatch for {asset}: expected {expected}, got {actual}", file=sys.stderr)
        return 1

    TARGET_DIR.mkdir(exist_ok=True)
    target.write_bytes(binary)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    version = installed_version(target)
    if version != VERSION:
        print(f"downloaded binary reports version {version!r}, expected {VERSION}", file=sys.stderr)
        return 1
    print(f"opa {VERSION} installed at {target} (sha256 verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
