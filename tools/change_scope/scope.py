"""Classify a change as documentation only or full, and judge an aggregate gate by that scope.

A product repository lists its documentation in `.github/change-scope.json`. A change is documentation only when
every changed path matches one of those patterns and none is a path that can change a build, a test or a package.
Anything else, and every event that is not a pull request, is a full change.
"""

from __future__ import annotations

import bisect
import fnmatch
import json
import os
import posixpath
import re
import subprocess
import xml.etree.ElementTree as ElementTree
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

POLICY_PATH = Path(".github/change-scope.json")
POLICY_FIELDS = {"schemaVersion", "documentation"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
# WHY: These paths decide what is built, tested, packed or run, so a change to them is never documentation only,
# whatever the policy lists; .github/** also holds the policy itself, so a pull request cannot reclassify itself.
NEVER_DOCUMENTATION = (".github/**", "global.json", "**/*.cs", "**/*.csproj", "**/*.fsproj", "**/*.vbproj",
                       "**/*.sqlproj", "**/*.props", "**/*.targets", "**/*.proj", "**/*.projitems", "**/*.shproj",
                       "**/*.slnx", "**/*.sln", "**/*.json", "**/*.py", "**/*.sh", "**/*.yml", "**/*.yaml",
                       "**/.editorconfig", "**/*.globalconfig", "**/*.ruleset", "**/*.snk", "**/*.rsp", "**/*.resx",
                       "**/nuget.config", "**/NuGet.Config")
PROJECT_SUFFIXES = (".csproj", ".fsproj", ".vbproj", ".sqlproj")
IMPORT_SUFFIXES = (".props", ".targets", ".proj", ".projitems", ".shproj")
MSBUILD_SUFFIXES = PROJECT_SUFFIXES + IMPORT_SUFFIXES
EXCLUDED_DIRECTORIES = {"bin", "obj", "artifacts", ".git"}
# Items that name packages, projects or namespaces rather than files.
REFERENCE_ITEMS = {"PackageReference", "PackageVersion", "GlobalPackageReference", "PackageDownload",
                   "ProjectReference", "FrameworkReference", "Using", "InternalsVisibleTo", "AssemblyAttribute",
                   "Folder"}
PROPERTY = re.compile(r"[$@%]\([^)]*\)")
URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"'<>()]+")
# A stem or directory name counts only between quote, slash, backslash, angle-bracket or equals characters, as in
# Path.Combine(root, "docs"), root / "docs", <DocDir>docs</DocDir> or Include="..\docs\x".
PATH_BOUNDARY = r"[\"'`/<>=]"
# Marks a project-relative value that continues $(MSBuildProjectDirectory) without a separator.
GLUED = "\0"
# WHY: A bound against reading large data files; text that opens a file by name is far smaller.
MAX_SCANNED_BYTES = 5 * 1024 * 1024
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


def tracked_files(root: Path) -> list[str]:
    """Return the files Git tracks below root; every file when root is not a Git checkout."""
    completed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=120,
                               check=False)
    if completed.returncode == 0:
        return sorted(name.decode("utf-8") for name in completed.stdout.split(b"\0") if name)

    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def build_inputs(root: Path, files: list[str] | None = None) -> dict:
    """Return what MSBuild can read: project directories, referenced paths, globs, file-name patterns and links."""
    files = tracked_files(root) if files is None else files
    inputs = {"directories": set(), "paths": set(), "globs": [], "names": set(), "projectRelative": set(),
              "candidates": set(), "nameTemplates": set(), "links": _links(root, files),
              "linkPaths": {name for name in files if (root / name).is_symlink()}}
    documents, definitions = [], {}
    for name in files:
        project = root / name
        relative = PurePosixPath(name)
        if project.suffix not in MSBUILD_SUFFIXES or project.is_symlink() or not project.is_file():
            continue

        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts[:-1]):
            continue

        # WHY: SDK-style projects include the files of their directory by default (Compile, EmbeddedResource,
        # None), so nothing below a project file is documentation only.
        # https://learn.microsoft.com/dotnet/core/project-sdk/overview#default-includes-and-excludes
        if project.suffix in PROJECT_SUFFIXES:
            inputs["directories"].add(relative.parent.as_posix())

        try:
            document = ElementTree.parse(project).getroot()
        except ElementTree.ParseError as error:
            raise ScopeError(f"{relative} is not valid MSBuild XML.") from error

        documents.append((project, document))
        for group in document.iter():
            if group.tag.split("}")[-1] == "PropertyGroup":
                for prop in group:
                    definitions.setdefault(prop.tag.split("}")[-1].lower(), set()).add((prop.text or "").strip())

    for project, document in documents:
        parents = {child: parent for parent in document.iter() for child in parent}
        for element in document.iter():
            parent = parents.get(element)
            is_item = parent is not None and parent.tag.split("}")[-1] == "ItemGroup"
            if is_item and element.tag.split("}")[-1] not in REFERENCE_ITEMS:
                for attribute in ("Include", "Update"):
                    for value in (element.attrib.get(attribute) or "").split(";"):
                        _add_input(inputs, root, project.parent, value.strip())

            # WHY: Properties and task parameters name files too (CodeAnalysisRuleSet, ReadLinesFromFile File=...);
            # any literal value that names an existing file or directory of the repository counts as an input.
            values = [*(element.attrib.values()), *([element.text] if len(element) == 0 and element.text else [])]
            for text in values:
                for value in text.split(";"):
                    _add_candidate(inputs, root, project.parent, value.strip())

    _resolve_project_relative(inputs, root)
    _expand_name_templates(inputs, root, files, definitions)
    _follow_links(inputs)
    inputs["globs"] = list(dict.fromkeys(inputs["globs"]))
    # One expression for every glob and file-name pattern: matching them one by one recompiles each pattern for
    # every path (MySql yields thousands of globs from project-relative includes).
    inputs["globMatch"] = _union([glob_regex(pattern) for pattern in inputs["globs"]])
    inputs["nameMatch"] = _union([fnmatch.translate(name) for name in sorted(inputs["names"])])
    return inputs


def glob_regex(pattern: str) -> str:
    """Translate a path glob into a regular expression with the meaning of PurePath.full_match."""
    parts = []
    segments = pattern.split("/")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            parts.append(".*" if last else "(?:[^/]*/)*")
            continue

        piece = "".join("[^/]*" if character == "*" else "[^/]" if character == "?" else re.escape(character)
                        for character in segment)
        parts.append(piece if last else f"{piece}/")

    return "".join(parts)


def _union(expressions: list[str]) -> re.Pattern | None:
    """Compile alternatives into one full-match pattern; None when there are none."""
    return re.compile("|".join(f"(?:{expression})" for expression in expressions)) if expressions else None


def _expand_name_templates(inputs: dict, root: Path, files: list[str], definitions: dict[str, set[str]]) -> None:
    """Turn file names built from properties into the names the repository can give them."""
    texts = None
    for template in sorted(inputs.pop("nameTemplates")):
        # An item list (@(Compile)) or metadata (%(Filename)) in a file name names files MSBuild derives from other
        # items, which are recorded themselves; anything else MSBuild could only compute stays unknown.
        if re.fullmatch(r"@\([^)]*\)", template):
            continue

        if "@(" in template or "%(" in template:
            inputs["names"].add("*")
            continue

        # A property can stand for a directory prefix ("$(RepositoryRoot)NOTICE.md"), so the literal rest after the
        # last property is a file name of its own unless it is only an extension.
        tail = template.rsplit(")", 1)[-1]
        if tail and not tail.startswith("."):
            inputs["names"].add(tail)

        names = {template}
        for prop in re.findall(r"\$\(([^)]*)\)", template):
            # WHY: A property a script or workflow sets ("-p:DocName=x.md", "DocName: x.md") can name any file, even
            # when a project file defines a default: "These global property values override property values that
            # are set in the project file."
            # https://learn.microsoft.com/visualstudio/msbuild/msbuild-properties#global-properties
            if texts is None:
                texts = _texts(root, files)

            setter = re.compile(rf"(?<![a-z0-9_]){re.escape(prop.lower())}\s*[=:]")
            if any(setter.search(text) for text in texts.values()):
                inputs["names"].add("*")
                names = set()
                break

            values = definitions.get(prop.lower())
            if values is None:
                # WHY: A property neither the repository's MSBuild files nor its scripts set comes from the .NET SDK
                # or MSBuild itself (ProjectDepsFilePath names a generated .deps.json) and names no tracked file.
                names = set()
                break

            if any(PROPERTY.search(value) for value in values):
                inputs["names"].add("*")
                names = set()
                break

            names = {name.replace(f"$({prop})", value) for name in names for value in values}

        inputs["names"].update(name for name in names if name)


def _texts(root: Path, files: list[str]) -> dict[str, str]:
    """Return the lowered text of every tracked text file outside MSBuild files."""
    texts = {}
    for name in files:
        path = root / name
        if path.suffix in MSBUILD_SUFFIXES or path.is_symlink():
            continue

        try:
            data = path.read_bytes()
        except OSError:
            continue

        if len(data) <= MAX_SCANNED_BYTES and b"\0" not in data[:8000]:
            texts[name] = data.decode("utf-8", "ignore").lower()

    return texts


def _links(root: Path, files: list[str]) -> dict[str, str]:
    """Return every tracked symbolic link that points inside the repository, as link path to target path."""
    links, resolved_root = {}, root.resolve()
    for name in files:
        path = root / name
        if not path.is_symlink():
            continue

        try:
            links[name] = path.resolve().relative_to(resolved_root).as_posix()
        except ValueError:
            continue

    return links


def _resolve_project_relative(inputs: dict, root: Path) -> None:
    """Resolve project-relative paths and property candidates against every project directory."""
    # WHY: $(MSBuildProjectDirectory) is the directory of the project being built, and a file imported by several
    # projects resolves once per project; a relative path can leave that directory ("../../docs/rules.md"), so it
    # is resolved against every project directory, which covers each real resolution.
    # https://learn.microsoft.com/visualstudio/msbuild/msbuild-reserved-and-well-known-properties
    for relative in sorted(inputs.pop("projectRelative")):
        for directory in sorted(inputs["directories"]):
            if relative.startswith(GLUED):
                # The project directory has no final separator; the root project's absolute path would continue
                # outside the repository.
                pattern = None if directory == "." else _within(".", directory + relative[len(GLUED):])
            else:
                pattern = _within(directory, relative)

            if pattern is None:
                continue

            if any(character in pattern for character in "*?"):
                inputs["globs"].append(pattern)
            else:
                inputs["paths"].add(pattern)

    for base, value in sorted(inputs.pop("candidates")):
        for directory in sorted({base, *inputs["directories"]}):
            path = _within(directory, value)
            if path is None or not (root / path).exists():
                continue

            if (root / path).is_dir():
                # WHY: A task that receives a directory can read anything below it, the root and directories that
                # hold projects included; Exec, for example, runs any command with it.
                # https://learn.microsoft.com/visualstudio/msbuild/exec-task
                inputs["globs"].append("**" if path == "." else f"{path}/**")
            else:
                inputs["paths"].add(path)


def _follow_links(inputs: dict) -> None:
    """Add the target spelling of every input path or glob that passes through a symbolic link."""
    for link, target in sorted(inputs["links"].items()):
        for path in sorted(inputs["paths"]):
            if path == link or path.startswith(f"{link}/"):
                inputs["paths"].add(target + path[len(link):])

        for pattern in list(inputs["globs"]):
            if pattern.startswith(f"{link}/"):
                inputs["globs"].append(target + pattern[len(link):])


def _within(directory: str, value: str) -> str | None:
    """Join a relative value to a repository directory; None when the result leaves the repository."""
    path = posixpath.normpath(value if directory == "." else f"{directory}/{value}")
    return None if path == ".." or path.startswith("../") else path


def _add_candidate(inputs: dict, root: Path, directory: Path, value: str) -> None:
    """Record a literal property value that may name a file, relative to its file or to any project."""
    value = unquote(value.replace("\\", "/"))
    if value.startswith("$(MSBuildProjectDirectory)") and not value.startswith("$(MSBuildProjectDirectory)/"):
        rest = value.removeprefix("$(MSBuildProjectDirectory)")
        if rest and not PROPERTY.search(rest) and not any(character in rest for character in "*?<>|\"\n"):
            for project in inputs["directories"] | {directory.resolve().relative_to(root.resolve()).as_posix()}:
                if project != ".":
                    inputs["candidates"].add((".", project + rest))

        return

    value = value.removeprefix("$(MSBuildThisFileDirectory)").removeprefix("$(MSBuildProjectDirectory)").lstrip("/")
    if not value or PROPERTY.search(value) or any(character in value for character in "*?<>|\"\n"):
        return

    inputs["candidates"].add((directory.resolve().relative_to(root.resolve()).as_posix(), value))


def _add_input(inputs: dict, root: Path, directory: Path, value: str) -> None:
    """Record one item path, resolved from its own file and from every project."""
    value = unquote(value.replace("\\", "/"))
    this_file = value.startswith("$(MSBuildThisFileDirectory)")
    project_file = value.startswith("$(MSBuildProjectDirectory)")
    # WHY: MSBuildThisFileDirectory includes the final separator, so "$(MSBuildThisFileDirectory)/../x" is still
    # relative; MSBuildProjectDirectory does not, so "$(MSBuildProjectDirectory)Notice.md" continues the directory
    # name itself (src/CoreNotice.md).
    # https://learn.microsoft.com/visualstudio/msbuild/msbuild-reserved-and-well-known-properties
    if project_file and not value.startswith("$(MSBuildProjectDirectory)/") and not PROPERTY.search(
            value.removeprefix("$(MSBuildProjectDirectory)")):
        inputs["projectRelative"].add(GLUED + value.removeprefix("$(MSBuildProjectDirectory)"))
        return

    value = value.removeprefix("$(MSBuildThisFileDirectory)").removeprefix("$(MSBuildProjectDirectory)").lstrip("/")
    if not value:
        return

    # WHY: A path built from another property cannot be resolved without MSBuild; every file whose name matches
    # what is left counts as a build input ("*.md" of "$(DocDir)/**/*.md" matches every Markdown file), and a name
    # that is itself a property matches every file, which can only make more changes full.
    # https://learn.microsoft.com/visualstudio/msbuild/msbuild-items
    if PROPERTY.search(value):
        name = PurePosixPath(value).name
        if PROPERTY.search(name):
            inputs["nameTemplates"].add(name)
        elif name:
            inputs["names"].add(name)

        return

    # WHY: "The Include attribute is a path that is interpreted relative to the project file's folder [...] even if
    # the item is in an imported file such as a .targets file", and a file can be both a project and an import;
    # only $(MSBuildThisFileDirectory) names the folder of the file itself. A literal path is therefore resolved
    # from its own file and from every project, which covers each real resolution.
    # https://learn.microsoft.com/visualstudio/msbuild/msbuild-items
    # https://learn.microsoft.com/visualstudio/msbuild/import-element-msbuild
    if not this_file:
        inputs["projectRelative"].add(value)

    if project_file:
        return

    base = directory.resolve().relative_to(root.resolve()).as_posix()
    if any(character in value for character in "*?"):
        pattern = _within(base, value)
        if pattern is not None:
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

    # WHY: A tracked symbolic link can point anywhere, inside or outside the checkout, so a changed link is never
    # documentation, whatever its target.
    name_match, glob_match = inputs.get("nameMatch"), inputs.get("globMatch")
    if (path in inputs["paths"] or path in inputs["linkPaths"]
            or (name_match is not None and name_match.fullmatch(pure.name))
            or (glob_match is not None and glob_match.fullmatch(path))
            or any(directory == "." or path.startswith(f"{directory}/") for directory in inputs["directories"])):
        return False

    # WHY: full_match treats "**" as any number of directories, so "docs/**" covers nested files.
    # https://docs.python.org/3/library/pathlib.html#pathlib.PurePath.full_match
    return any(pure.full_match(pattern) for pattern in patterns)


def changed_paths_and_links(root: Path, base: str, head: str) -> tuple[list[str], set[str]]:
    """Return changed paths and the paths that were symbolic links on either side of the diff."""
    if SHA.fullmatch(base) is None or SHA.fullmatch(head) is None:
        raise ScopeError("Base and head must be full object names.")

    # WHY: Raw records include both file modes, so a deleted or replaced symlink still counts as a link.
    # --no-renames gives each changed path its own record, including both sides of a rename.
    # https://git-scm.com/docs/git-diff#_raw_output_format
    completed = subprocess.run(["git", "-C", str(root), "diff", "--raw", "-z", "--no-renames", base, head],
                               capture_output=True, timeout=120, check=False)
    if completed.returncode:
        raise ScopeError(f"Cannot compare {base} with {head}.")

    paths, links = [], set()
    fields = completed.stdout.split(b"\0")[:-1]
    for header, raw_path in zip(fields[::2], fields[1::2], strict=True):
        path = raw_path.decode("utf-8")
        paths.append(path)
        old_mode, new_mode, _ = header[1:].split(b" ", 2)
        if b"120000" in (old_mode, new_mode):
            links.add(path)

    return sorted(paths), links


def references(root: Path, files: list[str], patterns: list[str], candidates: list[str]) -> dict[str, list[str]]:
    """Return, for each candidate, the tracked non-documentation files that mention it as a path or path part."""
    if not candidates:
        return {}

    texts = {}
    for name in files:
        # WHY: Markdown links and documentation files read nothing; every other tracked file, code, tests, scripts,
        # MSBuild and workflow files, may open a documentation file by name.
        # The policy is the contract that names the documentation, not a file that reads it; a file with a
        # protected extension (docs/check.py) can read one even below a documentation path.
        pure = PurePosixPath(name)
        protected = any(pure.full_match(pattern) for pattern in NEVER_DOCUMENTATION)
        if (name == POLICY_PATH.as_posix() or name.lower().endswith(".md")
                or (not protected and any(pure.full_match(pattern) for pattern in patterns))):
            continue

        path = root / name
        try:
            if path.is_symlink():
                content = os.readlink(path)
            else:
                data = path.read_bytes()
                if len(data) > MAX_SCANNED_BYTES or b"\0" in data[:8000]:
                    continue

                content = data.decode("utf-8", "ignore")
        except OSError:
            continue

        # WHY: MSBuild unescapes %XX in item specifications ("guide%3Bv2.md" is guide;v2.md), and a URL such as
        # https://github.com/.../blob/main/docs/x.md names a web page, not a file of the checkout.
        # https://learn.microsoft.com/visualstudio/msbuild/special-characters-to-escape
        lowered = content.lower()
        texts[name] = URL.sub(" ", f"{lowered}\n{unquote(lowered)}").replace("\\", "/")

    # One text with file offsets: each token is searched once over everything, not once per file, and the
    # directory token shared by every file below docs/ is searched only once.
    names, starts, chunks, offset = [], [], [], 0
    for name, text in texts.items():
        names.append(name)
        starts.append(offset)
        chunks.append(text)
        offset += len(text) + 1

    corpus = "\0".join(chunks)
    cache: dict[tuple[str, bool], list[str]] = {}

    def mentioned(token: str, bounded: bool) -> list[str]:
        key = (token, bounded)
        if key not in cache:
            expression = re.compile(f"{PATH_BOUNDARY}{re.escape(token)}{PATH_BOUNDARY}" if bounded
                                    else re.escape(token))
            found, position = [], 0
            while (match := expression.search(corpus, position)) is not None:
                index = bisect.bisect_right(starts, match.start()) - 1
                found.append(names[index])
                # One mention is enough; continue in the next file.
                position = starts[index + 1] if index + 1 < len(starts) else len(corpus)

            cache[key] = found

        return cache[key]

    result = {}
    for candidate in candidates:
        pure = PurePosixPath(candidate.lower())
        found = dict.fromkeys(name for index in range(len(pure.parts))
                              for name in mentioned(PurePosixPath(*pure.parts[index:]).as_posix(), False))
        for part in sorted({pure.stem, *pure.parts[:-1]}):
            if part:
                found.update(dict.fromkeys(mentioned(part, True)))

        if found:
            result[candidate] = sorted(found)[:5]

    return result


def classify(root: Path, base: str, head: str) -> dict:
    """Return the scope of the change from base to head, judged by the policy and the tracked files at root."""
    patterns = load_policy(root)
    paths, links = changed_paths_and_links(root, base, head)
    files = tracked_files(root)
    inputs = build_inputs(root, files)
    inputs["linkPaths"].update(links)
    others = [path for path in paths if not is_documentation(path, patterns, inputs)]
    # WHY: Static MSBuild evaluation cannot see a task or test that opens a file by name, so a documentation file
    # stays documentation only while nothing else in the repository names it, its file name, its stem or one of its
    # directories as a path part. Any such mention makes the change full.
    referenced = references(root, files, patterns, [path for path in paths if path not in others]) if patterns else {}
    others = sorted({*others, *referenced})
    scope = "docs" if patterns and paths and not others else "full"
    return {"scope": scope, "paths": len(paths), "nonDocumentation": others[:20],
            "references": {name: referenced[name] for name in sorted(referenced)[:20]}}


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
