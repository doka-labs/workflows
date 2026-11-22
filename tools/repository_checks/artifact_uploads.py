"""Check that every artifact upload in this repository's workflows names individual text evidence files.

WHY: Uploading a whole tool output directory has carried build by-products into
evidence twice in the doka-labs repositories. The NuGet HTTP cache in the audit
evidence failed uploads in MySql, SafeMigrations and NestedSet in October 2026
(cache file names contain ':'). In SafeMigrations three self-contained EF
migration bundles of 60.3 MB each, uploaded uncompressed in every database cell
of every CI run, made up 181 of 194 MB per artifact and about 53 GB of
artifact storage within 14 days (November 2026). Naming each file, limiting
uploads to text formats and keeping compression on prevents both.
"""

from __future__ import annotations

import re
from pathlib import Path

TEXT_EXTENSIONS = frozenset((".json", ".log", ".trx", ".txt", ".sql", ".md", ".xml", ".csv", ".sarif"))
GLOB_CHARACTERS = re.compile(r"[*?\[\]]")
UPLOAD = re.compile(r"^(?P<indent> *)(?:- )?uses: actions/upload-artifact@", re.MULTILINE)


class UploadError(RuntimeError):
    """An artifact upload would not be limited to named text evidence files."""


def _with_block(lines: list[str], start: int, indent: int) -> list[str]:
    """Return the lines of the step's `with:` mapping that follows the `uses:` line."""
    block, inside, key_indent = [], False, None
    for line in lines[start + 1:]:
        stripped = line.strip()
        current = len(line) - len(line.lstrip(" "))
        if stripped and current <= indent - 2:
            break

        if stripped.startswith("- ") and current <= indent:
            break

        if not inside:
            if stripped == "with:":
                inside, key_indent = True, current + 2

            continue

        if stripped and current < key_indent:
            break

        block.append(line)

    return block


def _paths(block: list[str]) -> list[str]:
    """Return the entries of the `path:` input, as a single value or a literal block."""
    for index, line in enumerate(block):
        match = re.match(r"^( *)path:\s*(.*)$", line)
        if not match:
            continue

        value = match[2].strip()
        if value and value != "|":
            return [value]

        indent = len(match[1])
        entries = []
        for entry in block[index + 1:]:
            if entry.strip() and len(entry) - len(entry.lstrip(" ")) <= indent:
                break

            if entry.strip():
                entries.append(entry.strip())

        return entries

    raise UploadError("An upload step has no path input.")


def check_workflow(text: str, label: str) -> int:
    """Check every upload step of one workflow and return how many were checked."""
    lines = text.splitlines()
    count = 0
    for number, line in enumerate(lines):
        match = UPLOAD.match(line)
        if not match:
            continue

        count += 1
        indent = len(match["indent"]) + (2 if line.lstrip().startswith("- ") else 0)
        block = _with_block(lines, number, indent)
        where = f"{label}:{number + 1}"
        for entry in block:
            if re.match(r"^\s*compression-level:\s*['\"]?0['\"]?\s*$", entry):
                raise UploadError(f"{where}: compression-level 0 uploads evidence uncompressed.")

        for path in _paths(block):
            name = path.rsplit("/", 1)[-1]
            if GLOB_CHARACTERS.search(path) or path.startswith("!"):
                raise UploadError(f"{where}: '{path}' is a pattern; name each evidence file.")

            if ".." in path.split("/") or path.endswith("/"):
                raise UploadError(f"{where}: '{path}' is not a single file inside the workspace.")

            if Path(name).suffix.lower() not in TEXT_EXTENSIONS:
                raise UploadError(f"{where}: '{path}' is not a text evidence file "
                                  f"({', '.join(sorted(TEXT_EXTENSIONS))}).")

    return count


def check_repository(root: Path) -> int:
    """Check all workflows of the repository and return the number of upload steps."""
    total = 0
    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        total += check_workflow(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())

    return total
