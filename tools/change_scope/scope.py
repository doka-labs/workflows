"""Classify a change as documentation only or full, and judge an aggregate gate by that scope.

A product repository lists its documentation in `.github/change-scope.json`. A change is documentation only when
every changed path matches one of those patterns and none is a path that can change a build, a test or a package.
Anything else, and every event that is not a pull request, is a full change.
"""

from __future__ import annotations

import json
import posixpath
import re
import subprocess
import xml.etree.ElementTree as ElementTree
from pathlib import Path, PurePosixPath

POLICY_PATH = Path(".github/change-scope.json")
POLICY_FIELDS = {"schemaVersion", "documentation"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
# WHY: These paths decide what is built, tested, packed or run, so a change to them is never documentation only,
# whatever the policy lists; .github/** also holds the policy itself, so a pull request cannot reclassify itself.
NEVER_DOCUMENTATION = (".github/**", "global.json", "**/*.cs", "**/*.csproj", "**/*.props", "**/*.targets",
                       "**/*.slnx", "**/*.sln", "**/*.json", "**/*.py", "**/*.sh", "**/*.yml", "**/*.yaml",
                       "**/.editorconfig", "**/nuget.config", "**/NuGet.Config")
MSBUILD_SUFFIXES = (".csproj", ".props", ".targets")
EXCLUDED_DIRECTORIES = {"bin", "obj", "artifacts", ".git"}
# Items that name packages, projects or namespaces rather than files.
REFERENCE_ITEMS = {"PackageReference", "PackageVersion", "GlobalPackageReference", "PackageDownload",
                   "ProjectReference", "FrameworkReference", "Using", "InternalsVisibleTo", "AssemblyAttribute",
                   "Folder"}
PROPERTY = re.compile(r"[$@%]\([^)]*\)")
SCOPES = ("docs", "full")


class ScopeError(RuntimeError):
    """The policy, the repository or the gate results cannot prove a scope."""


def load_policy(root: Path) -> list[str]:
    """Read and validate the documentation patterns of the repository; without a policy nothing is documentation."""
    path = root / POLICY_PATH
    if not path.is_file():
        return []

    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ScopeError(f"Cannot read {POLICY_PATH}.") from error

    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS or policy["schemaVersion"] != 1:
        raise ScopeError(f"{POLICY_PATH} must use schemaVersion 1 with exactly: documentation.")

    patterns = policy["documentation"]
    if (not isinstance(patterns, list) or not patterns or len(set(patterns)) != len(patterns)
            or any(not isinstance(item, str) or not item or item.startswith("/") or ".." in item.split("/")
                   for item in patterns)):
        raise ScopeError(f"{POLICY_PATH} documentation must be distinct relative path patterns.")

    return patterns


def build_inputs(root: Path) -> dict:
    """Return what MSBuild can read: project directories, referenced paths, item globs and unresolved file names."""
    inputs = {"directories": set(), "paths": set(), "globs": [], "names": set()}
    for project in sorted(root.rglob("*")):
        if project.suffix not in MSBUILD_SUFFIXES or not project.is_file():
            continue

        relative = project.relative_to(root)
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts[:-1]):
            continue

        # WHY: SDK-style projects include the files of their directory by default (Compile, EmbeddedResource,
        # None), so nothing below a project file is documentation only.
        # https://learn.microsoft.com/dotnet/core/project-sdk/overview#default-includes-and-excludes
        if project.suffix == ".csproj":
            inputs["directories"].add(relative.parent.as_posix())

        try:
            document = ElementTree.parse(project).getroot()
        except ElementTree.ParseError as error:
            raise ScopeError(f"{relative} is not valid MSBuild XML.") from error

        for group in document.iter():
            if group.tag.split("}")[-1] != "ItemGroup":
                continue

            for item in group:
                if item.tag.split("}")[-1] in REFERENCE_ITEMS:
                    continue

                for attribute in ("Include", "Update"):
                    for value in (item.attrib.get(attribute) or "").split(";"):
                        _add_input(inputs, root, project.parent, value.strip())

    return inputs


def _add_input(inputs: dict, root: Path, directory: Path, value: str) -> None:
    """Record one item path relative to its MSBuild file."""
    value = value.replace("\\", "/").replace("$(MSBuildThisFileDirectory)", "")
    if not value:
        return

    # WHY: $(MSBuildProjectDirectory) is the directory of each project, and nothing below a project is
    # documentation anyway (NestedSet packs $(MSBuildProjectDirectory)/README.md when a project has one).
    # https://learn.microsoft.com/visualstudio/msbuild/msbuild-reserved-and-well-known-properties
    project_relative = value.removeprefix("$(MSBuildProjectDirectory)")
    if project_relative != value and not PROPERTY.search(project_relative):
        return

    # WHY: A path built from another property cannot be resolved without MSBuild; every file of that name then
    # counts as a build input, which can only make more changes full.
    if PROPERTY.search(value):
        name = PurePosixPath(PROPERTY.sub("/", value)).name
        if name:
            inputs["names"].add(name)

        return

    base = directory.resolve().relative_to(root.resolve()).as_posix()
    if any(character in value for character in "*?"):
        pattern = posixpath.normpath(value if base == "." else f"{base}/{value}")
        if pattern != ".." and not pattern.startswith("../"):
            inputs["globs"].append(pattern)

        return

    try:
        inputs["paths"].add((directory / value).resolve().relative_to(root.resolve()).as_posix())
    except ValueError:
        return


def is_documentation(path: str, patterns: list[str], inputs: dict) -> bool:
    """Return whether a changed path is documentation that no build, test or package reads."""
    pure = PurePosixPath(path)
    if any(pure.full_match(pattern) for pattern in NEVER_DOCUMENTATION):
        return False

    if (path in inputs["paths"] or pure.name in inputs["names"]
            or any(pure.full_match(pattern) for pattern in inputs["globs"])
            or any(directory == "." or path.startswith(f"{directory}/") for directory in inputs["directories"])):
        return False

    # WHY: full_match treats "**" as any number of directories, so "docs/**" covers nested files.
    # https://docs.python.org/3/library/pathlib.html#pathlib.PurePath.full_match
    return any(pure.full_match(pattern) for pattern in patterns)


def changed_paths(root: Path, base: str, head: str) -> list[str]:
    """Return every path that differs between two commits or trees, renames as both of their paths."""
    if SHA.fullmatch(base) is None or SHA.fullmatch(head) is None:
        raise ScopeError("Base and head must be full object names.")

    # WHY: --no-renames reports a rename as the deleted and the added path, so both are classified.
    # https://git-scm.com/docs/git-diff#Documentation/git-diff.txt---no-renames
    completed = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "-z", "--no-renames", base, head],
                               capture_output=True, timeout=120, check=False)
    if completed.returncode:
        raise ScopeError(f"Cannot compare {base} with {head}.")

    return sorted(name.decode("utf-8") for name in completed.stdout.split(b"\0") if name)


def classify(root: Path, base: str, head: str) -> dict:
    """Return the scope of the change from base to head, judged by the policy and MSBuild files at root."""
    patterns = load_policy(root)
    paths = changed_paths(root, base, head)
    inputs = build_inputs(root)
    others = [path for path in paths if not is_documentation(path, patterns, inputs)]
    scope = "docs" if patterns and paths and not others else "full"
    return {"scope": scope, "paths": len(paths), "nonDocumentation": others[:20]}


def judge(results: dict, scope: str, skippable: list[str]) -> list[str]:
    """Return the problems of an aggregate gate: every job succeeded or was skipped for a documentation change."""
    problems = []
    if scope not in SCOPES:
        problems.append(f"Unknown scope {scope!r}.")

    if not isinstance(results, dict) or not results:
        return [*problems, "The gate received no job results."]

    unknown = sorted(set(skippable) - set(results))
    if unknown:
        problems.append(f"Skippable jobs are not needed by the gate: {', '.join(unknown)}.")

    for name, entry in sorted(results.items()):
        result = entry.get("result") if isinstance(entry, dict) else None
        if result == "success":
            continue

        if result == "skipped" and scope == "docs" and name in skippable:
            continue

        problems.append(f"{name} ended with {result}.")

    return problems
