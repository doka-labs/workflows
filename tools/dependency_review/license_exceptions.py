"""Restrict license exceptions of a dependency review to their exact reviewed NuGet versions.

`actions/dependency-review-action` exempts packages listed in
`allow-dependencies-licenses` from the license check at every version: its
`purlsMatch` compares only the package type and name, not the version
(src/purl.ts of v5.0.1). This check reads the action's complete comparison and
fails when an exempt package appears at any version other than the reviewed one.

https://github.com/actions/dependency-review-action/blob/v5.0.1/src/purl.ts
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

EXCEPTION_PATTERN = re.compile(r"pkg:nuget/([^@/?#]+)@([^@/?#]+)")


class LicenseExceptionError(RuntimeError):
    """A license exception is malformed or a dependency change does not match its reviewed version."""


def parse_exceptions(text: str) -> dict[str, str]:
    """Parse comma-separated `pkg:nuget/Name@version` entries into lower-case name to version."""
    approved = {}
    for entry in text.split(","):
        entry = entry.strip()
        exact = EXCEPTION_PATTERN.fullmatch(entry)
        if exact is None:
            raise LicenseExceptionError(f"License exception must be pkg:nuget/Name@version: {entry}")

        approved[exact[1].lower()] = exact[2]

    return approved


def action_name(purl: str) -> str | None:
    """Return the lower-case NuGet identity the action derives from a package URL, if any."""
    # WHY: Mirrors the action's own purl parsing, including its namespace-only fallback, so a
    # package cannot escape the check through an unusual but accepted package URL.
    kind = re.search(r"pkg:([a-zA-Z0-9_-]+)/.*", purl)
    if not purl.startswith("pkg:") or kind is None or kind[1].lower() != "nuget":
        return None

    parts = purl.split("/")
    if len(parts) < 2 or not parts[1]:
        return None

    namespace = unquote(parts[1], errors="strict") if len(parts) > 2 else None
    name = re.search(r"([^@#?]+)[@#?]?.*", "/".join(parts[2:]) if namespace else parts[1])
    name = unquote(name[1], errors="strict") if name else None
    identity = f"{namespace}/{name}" if namespace and name else name or namespace

    return identity.lower() if identity else None


def check_changes(changes: object, approved: dict[str, str]) -> None:
    """Fail when an added dependency matches an exception at anything but its exact reviewed metadata."""
    if not isinstance(changes, list):
        raise LicenseExceptionError("Expected the complete dependency comparison array.")

    for change in changes:
        if not isinstance(change, dict) or change.get("change_type") not in ("added", "removed"):
            raise LicenseExceptionError("Invalid dependency comparison entry.")

        if change["change_type"] == "removed":
            continue

        name = (change.get("name") or "").lower()
        ecosystem = (change.get("ecosystem") or "").lower()
        raw_purl = change.get("package_url") or ""
        identity = name if ecosystem == "nuget" and name in approved else action_name(raw_purl)
        if identity not in approved:
            continue

        version = approved[identity]
        if (ecosystem != "nuget" or name != identity or change.get("version") != version
                or unquote(raw_purl, errors="strict").lower() != f"pkg:nuget/{identity}@{version}".lower()):
            raise LicenseExceptionError(
                f"License exception requires consistent NuGet {identity} {version} metadata. "
                "Review license files before approving another version.")


def read_changes(path: Path) -> object:
    """Read the action output, which the workflow stores as one JSON-encoded string."""
    try:
        with path.open(encoding="utf-8") as stream:
            transport = json.load(stream)

        return json.loads(transport) if isinstance(transport, str) else transport
    except (OSError, ValueError) as error:
        raise LicenseExceptionError("Dependency comparison is not readable JSON.") from error


def main(argv: list[str] | None = None) -> int:
    """Run the check from the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--changes", type=Path, required=True)
    parser.add_argument("--exceptions", required=True)
    args = parser.parse_args(argv)
    try:
        check_changes(read_changes(args.changes), parse_exceptions(args.exceptions))
    except LicenseExceptionError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1

    print("License exceptions are restricted to their exact reviewed versions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
