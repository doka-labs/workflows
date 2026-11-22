"""Generic checks of release candidate packages: archive safety, identity, source binding and symbols.

Product rules (allowed dependencies, payload layout, dependency ranges) stay
in the product repository's own qualification jobs.
"""

from __future__ import annotations

import hashlib
import io
import re
import stat
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path, PurePosixPath

SIGNATURE_ENTRY = ".signature.p7s"


class PackageError(RuntimeError):
    """A candidate package is unsafe, incomplete or bound to another identity."""


def require(condition: bool, message: str) -> None:
    """Raise a package error when a condition does not hold."""
    if not condition:
        raise PackageError(message)


def sha256(path: Path) -> str:
    """Return the SHA-256 of a file."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_entries(source: Path | bytes) -> dict[str, bytes]:
    """Read the regular entries of a package archive without extracting paths to disk."""
    # WHY: The same safety rules as NestedSet's packages.py: no absolute, parent, backslash or colon paths, no
    # case collisions, no links or encrypted entries.
    entries, seen = {}, set()
    try:
        with zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source) as archive:
            for entry in archive.infolist():
                name = entry.filename
                path = PurePosixPath(name)
                require(bool(name) and not path.is_absolute() and "\\" not in name and ":" not in name
                        and all(part not in ("", ".", "..") for part in name.rstrip("/").split("/"))
                        and all(ord(character) >= 32 for character in name), f"Unsafe archive path: {name!r}")
                key = name.rstrip("/").casefold()
                require(key not in seen, f"Duplicate or case-colliding archive path: {name}")
                seen.add(key)
                require(stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG, stat.S_IFDIR),
                        f"Archive entry is not a regular file: {name}")
                require(not entry.flag_bits & 1, f"Encrypted archive entry: {name}")
                if not entry.is_dir():
                    entries[name] = archive.read(entry)
    except (zipfile.BadZipFile, RuntimeError, OSError) as error:
        raise PackageError(f"Unreadable package archive: {error}") from error

    return entries


def canonical_payload(source: Path | bytes) -> dict[str, tuple[int, str]]:
    """Return name, size and SHA-256 of every entry except the repository signature."""
    # WHY: nuget.org's repository signature adds the entry .signature.p7s (present in the published
    # Doka.NestedSet 10.4.0 packages, absent from their unsigned candidates); every other entry must stay
    # byte-identical (NestedSet's canonical_payload).
    return {name: (len(content), hashlib.sha256(content).hexdigest())
            for name, content in sorted(archive_entries(source).items()) if name != SIGNATURE_ENTRY}


def nuspec(entries: dict[str, bytes]) -> ElementTree.Element:
    """Return the metadata element of the single root .nuspec."""
    specs = [name for name in entries if name.endswith(".nuspec") and "/" not in name]
    require(len(specs) == 1, "Expected exactly one root .nuspec.")
    try:
        root = ElementTree.fromstring(entries[specs[0]])
    except ElementTree.ParseError as error:
        raise PackageError(f"Invalid .nuspec: {error}") from error

    records = root.findall("{*}metadata")
    require(len(records) == 1, "Expected one .nuspec metadata element.")
    for field in ("id", "version", "repository"):
        require(len(records[0].findall("{*}" + field)) == 1, f"Expected one .nuspec {field}.")

    return records[0]


def inventory(directory: Path, version: str, repository: str, commit: str, frameworks: list[str]) -> list[dict]:
    """Check every primary and symbol package of a candidate and return their identities."""
    require(directory.is_dir() and not directory.is_symlink(), f"{directory} is not a package directory.")
    files = sorted(path for path in directory.iterdir() if path.suffix in (".nupkg", ".snupkg"))
    primaries = [path for path in files if path.suffix == ".nupkg"]
    require(bool(primaries), "The candidate contains no package.")
    require({path.with_suffix(".snupkg") for path in primaries} == {path for path in files if path.suffix == ".snupkg"},
            "Every package needs exactly one matching symbol package.")
    result = []
    for primary in primaries:
        symbols = primary.with_suffix(".snupkg")
        require(not primary.is_symlink() and not symbols.is_symlink(), "Packages must not be symbolic links.")
        primary_entries, symbol_entries = archive_entries(primary), archive_entries(symbols)
        require(SIGNATURE_ENTRY not in primary_entries, f"{primary.name} is already signed; candidates are unsigned.")
        record = nuspec(primary_entries)
        package_id = record.findtext("{*}id")
        require(primary.name == f"{package_id}.{version}.nupkg", f"{primary.name} is not {package_id} {version}.")
        for metadata, label in ((record, primary.name), (nuspec(symbol_entries), symbols.name)):
            require(metadata.findtext("{*}id") == package_id and metadata.findtext("{*}version") == version,
                    f"{label} names another package identity.")
            source = metadata.find("{*}repository")
            # WHY: The nuspec repository element lets consumers and nuget.org link the exact source commit.
            # https://learn.microsoft.com/nuget/reference/nuspec#repository
            require(source.get("type") == "git" and source.get("url") == f"https://github.com/{repository}"
                    and source.get("commit") == commit, f"{label} is not bound to {repository} at {commit}.")

        require(record.findtext("{*}readme") is not None, f"{primary.name} declares no readme.")
        license_node = record.find("{*}license")
        require(license_node is not None and license_node.get("type") in ("expression", "file"),
                f"{primary.name} declares no license.")
        symbol_type = nuspec(symbol_entries).find("{*}packageTypes/{*}packageType[@name='SymbolsPackage']")
        require(symbol_type is not None, f"{symbols.name} does not declare the SymbolsPackage type.")
        groups = [group.get("targetFramework") for group in record.findall("{*}dependencies/{*}group")]
        declared = {re.sub(r"^\.NETCoreApp([0-9]+)\.0$", r"net\1.0", value or "") for value in groups}
        require(declared <= set(frameworks), f"{primary.name} targets {sorted(declared)}, not {frameworks}.")
        dependencies = sorted({item.get("id") for item in record.findall("{*}dependencies/{*}group/{*}dependency")})
        result.append({"id": package_id, "version": version, "nupkg": primary.name, "snupkg": symbols.name,
                       "nupkgSha256": sha256(primary), "snupkgSha256": sha256(symbols),
                       "dependencies": dependencies})

    return dependency_order(result)


def dependency_order(inventory: list[dict]) -> list[dict]:
    """Order packages so that every package follows the candidate packages it depends on."""
    # WHY: NestedSet publishes dependencies before the packages that use them, so a consumer never sees a
    # package whose candidate dependency is not on nuget.org yet.
    remaining = {item["id"]: item for item in inventory}
    ordered = []
    while remaining:
        ready = sorted(name for name, item in remaining.items()
                       if not set(item["dependencies"]) & set(remaining) - {name})
        require(bool(ready), "The candidate packages depend on each other in a cycle.")
        for name in ready:
            ordered.append(remaining.pop(name))

    return ordered
