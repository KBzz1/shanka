#!/usr/bin/env python3
"""Structural self-check for a skill directory (stdlib only, skill-agnostic).

Usage: python quick_validate.py [SKILL_DIR]   (default: parent of this script's dir)

Checks:
 1. SKILL.md exists and parses as UTF-8;
 2. YAML frontmatter is delimited, single-block, and carries `name` + `description`;
 3. `name` matches the skill directory name (loading convention);
 4. `description` is a single non-empty line under ~500 chars (trigger text stays scannable);
 5. every relative Markdown link and inline-image target inside SKILL.md resolves;
 6. no machine-specific absolute paths are baked into any tracked text file
    (portability: the skill must not assume one developer's home directory).

Exit code 0 = pass; 1 = any failure (messages on stdout, one per finding).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_MAX_DESCRIPTION_CHARS = 500
_ABSOLUTE_PATH_HINTS = ("/home/", "/Users/", "C:\\Users\\", "/root/")


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter mapping, error). Values are raw scalars; no YAML dependency."""
    if not text.startswith("---\n"):
        return {}, "SKILL.md must start with a '---' frontmatter block"
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, "frontmatter is not closed by a '---' line"
    if text.find("\n---\n", end + 1) != -1:
        return {}, "multiple '---' blocks found; keep exactly one frontmatter block"
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            return {}, f"frontmatter line is not 'key: value': {line!r}"
        fields[key.strip()] = value.strip()
    return fields, ""


def _check_links(skill_dir: Path, text: str) -> list[str]:
    problems: list[str] = []
    pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)|<img[^>]+src=[\"']([^\"']+)[\"']")
    for match in pattern.finditer(text):
        target = match.group(1) or match.group(2)
        if re.match(r"^[a-z][a-z0-9+.-]*://", target) or target.startswith("#") or target.startswith("mailto:"):
            continue
        path = target.split("#", 1)[0]
        if path and not (skill_dir / path).is_file():
            problems.append(f"broken relative link target: {target}")
    return problems


def validate(skill_dir: Path) -> list[str]:
    problems: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return [f"SKILL.md not found under {skill_dir}"]
    text = skill_md.read_text(encoding="utf-8")

    fields, err = _frontmatter(text)
    if err:
        return [err]
    for key in ("name", "description"):
        if not fields.get(key):
            problems.append(f"frontmatter is missing '{key}'")
    if problems:
        return problems

    name = fields["name"]
    if name != skill_dir.name:
        problems.append(f"frontmatter name {name!r} != directory name {skill_dir.name!r}")
    description = fields["description"]
    if "\n" in description:
        problems.append("description must be a single line")
    if len(description) > _MAX_DESCRIPTION_CHARS:
        problems.append(f"description is {len(description)} chars (keep under {_MAX_DESCRIPTION_CHARS})")

    problems += _check_links(skill_dir, text)

    for file_path in sorted(skill_dir.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.name == "quick_validate.py":
            continue  # this checker defines the hint strings; it does not bake real paths
        try:
            content = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            continue  # binary assets (icons, images) are fine
        rel = file_path.relative_to(skill_dir)
        for hint in _ABSOLUTE_PATH_HINTS:
            if hint in content:
                problems.append(f"{rel} bakes a machine-specific path containing {hint!r}")
    return problems


def main() -> int:
    default_dir = Path(__file__).resolve().parent.parent
    skill_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default_dir
    problems = validate(skill_dir)
    for problem in problems:
        print(f"FAIL: {problem}")
    if not problems:
        print(f"OK: {skill_dir.name} passes structural validation")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
