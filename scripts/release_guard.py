"""Release consistency checks for local use and CI."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_INIT = ROOT / "src" / "discorsair" / "__init__.py"
VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$')
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[a-z]+\d+)?$")


def load_pyproject_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    version = str(data.get("project", {}).get("version", "") or "").strip()
    if not version:
        raise ValueError("project.version is missing in pyproject.toml")
    return version


def load_package_version() -> str:
    for line in PACKAGE_INIT.read_text(encoding="utf-8").splitlines():
        match = VERSION_RE.match(line.strip())
        if match is not None:
            return match.group(1)
    raise ValueError("discorsair.__version__ is missing in src/discorsair/__init__.py")


def normalize_tag(tag: str) -> str:
    value = tag.strip()
    if value.startswith("refs/tags/"):
        value = value[len("refs/tags/") :]
    if value.startswith("v"):
        value = value[1:]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="", help="Optional release tag, e.g. v0.1.0 or refs/tags/v0.1.0")
    args = parser.parse_args(argv)

    pyproject_version = load_pyproject_version()
    package_version = load_package_version()
    if pyproject_version != package_version:
        raise SystemExit(
            f"version mismatch: pyproject.toml={pyproject_version} src/discorsair/__init__.py={package_version}"
        )
    if not SEMVER_RE.fullmatch(pyproject_version):
        raise SystemExit(f"version must look like x.y.z or x.y.zrcN: {pyproject_version}")
    if args.tag:
        normalized_tag = normalize_tag(args.tag)
        if normalized_tag != pyproject_version:
            raise SystemExit(f"tag/version mismatch: tag={args.tag} version={pyproject_version}")
    print(f"release guard ok: version={pyproject_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
