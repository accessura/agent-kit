#!/usr/bin/env python3
"""Validate the customer-installable Accessura Skill bundle."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "accessura"
SKILL_FILE = SKILL_DIR / "SKILL.md"
REQUIRED_REFERENCES = {
    "references/authentication.md",
    "references/market-data.md",
    "references/trading.md",
}
FORBIDDEN_TEXT = (
    "scripts/agent-ecosystem",
    "web-demo-design",
    "orders_list",
    "sales_list",
    "packs_relist",
    "sectorSlugs",
)
PACKAGE_VERSION_FILES = (
    ROOT / "pyproject.toml",
    ROOT / "accessura_sdk" / "pyproject.toml",
)
TEXT_EXTENSIONS = {
    ".json",
    ".js",
    ".md",
    ".mjs",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}


def fail(message: str) -> None:
    print(f"skill bundle validation failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        fail("SKILL.md must start with YAML frontmatter")
    try:
        raw, _body = text[4:].split("\n---\n", 1)
    except ValueError:
        fail("SKILL.md frontmatter is not terminated")
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            fail(f"invalid frontmatter line: {line!r}")
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    if set(fields) != {"name", "description"}:
        fail("frontmatter must contain only name and description")
    if fields["name"] != "accessura":
        fail("frontmatter name must be accessura")
    if not fields["description"]:
        fail("frontmatter description must not be empty")
    return fields


def validate_links(text: str) -> None:
    linked_files: set[str] = set()
    for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
        if "://" in target or target.startswith("#"):
            continue
        relative = target.split("#", 1)[0]
        linked_files.add(relative)
        resolved = (SKILL_DIR / relative).resolve()
        try:
            resolved.relative_to(SKILL_DIR.resolve())
        except ValueError:
            fail(f"link escapes the bundle: {target}")
        if not resolved.is_file():
            fail(f"linked file does not exist: {target}")
    missing = REQUIRED_REFERENCES - linked_files
    if missing:
        fail(f"SKILL.md does not link required references: {sorted(missing)}")


def is_text_scan_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS


def forbidden_scan_files() -> tuple[Path, ...]:
    files = [
        SKILL_FILE,
        *(SKILL_DIR / "references").glob("*.md"),
        ROOT / "README.md",
        *(
            path
            for path in (ROOT / "examples").rglob("*")
            if is_text_scan_file(path)
        ),
    ]
    return tuple(sorted(set(files)))


def validate_forbidden_text(paths: tuple[Path, ...]) -> None:
    for path in paths:
        text = path.read_text(encoding="utf-8")
        try:
            label = path.relative_to(ROOT).as_posix()
        except ValueError:
            label = str(path)
        for forbidden in FORBIDDEN_TEXT:
            if forbidden in text:
                fail(f"{label} contains forbidden source/retired text: {forbidden}")


def project_version(path: Path) -> str:
    match = re.search(
        r'^version\s*=\s*"([^"]+)"\s*$',
        path.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if not match:
        fail(f"package version is missing from {path}")
    return match.group(1)


def validate_project_versions(
    skill_version: str,
    project_files: tuple[Path, ...] = PACKAGE_VERSION_FILES,
) -> None:
    for path in project_files:
        if project_version(path) != skill_version:
            fail(f"Skill VERSION must match the package version in {path}")


def main() -> None:
    if not SKILL_FILE.is_file():
        fail("accessura/SKILL.md is missing")
    text = SKILL_FILE.read_text(encoding="utf-8")
    parse_frontmatter(text)
    validate_links(text)
    version_file = SKILL_DIR / "VERSION"
    expected = {SKILL_FILE, SKILL_DIR / "agents/openai.yaml", version_file}
    expected.update(SKILL_DIR / item for item in REQUIRED_REFERENCES)
    missing = [str(path.relative_to(ROOT)) for path in expected if not path.is_file()]
    if missing:
        fail(f"required bundle files are missing: {missing}")
    skill_version = version_file.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", skill_version):
        fail("VERSION must contain a semantic version")
    validate_forbidden_text(forbidden_scan_files())
    validate_project_versions(skill_version)
    print("Accessura Skill bundle is valid")


if __name__ == "__main__":
    main()
