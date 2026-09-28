"""One product, one version.

The Python engine, the Swift app and the bundle all carry a version string; a
release where they disagree is a release where "which build is this?" has no
answer. This test is the tripwire.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    return re.search(r'^version = "([^"]+)"', text, re.MULTILINE).group(1)


def test_package_and_pyproject_agree():
    import aura

    assert aura.__version__ == _pyproject_version()


def test_swift_app_agrees():
    swift = (ROOT / "macos/Sources/AuraCore/Prefs.swift").read_text()
    assert f'public static let semantic = "{_pyproject_version()}"' in swift


def test_bundle_agrees():
    script = (ROOT / "scripts/make_app.sh").read_text()
    assert f'VERSION="{_pyproject_version()}"' in script


def test_apple_bundle_version_is_an_integer():
    script = (ROOT / "scripts/make_app.sh").read_text()
    assert re.search(r'^BUILD="\d+"$', script, re.MULTILINE)
