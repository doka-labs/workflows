"""Check that a checkout's SDK, target frameworks and framework-coupled packages fit its release line.

Two modes, chosen by `toolchain.coupling` in the release policy:

- `coupled`: package major, framework major and the major of every
  framework-coupled dependency are the same; one target framework per line.
- `independent`: the package major follows the package's own API; a line may
  target several frameworks, and each framework-coupled dependency must fit
  the major of the target framework it is used with.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from tools.release_lines import policy as policies

FRAMEWORK = re.compile(r"net([1-9][0-9]*)\.0\Z")
PROPERTY_REFERENCE = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)\Z")
FRAMEWORK_CONDITION = re.compile(r"\s*'\$\(TargetFramework\)'\s*==\s*'(net[1-9][0-9]*\.0)'\s*\Z")
# WHY: NestedSet narrows framework packages per shipping project with ItemGroup conditions on
# $(MSBuildProjectName), for example EF Core [10.0.12,11.0.0) for Doka.EntityFrameworkCore.NestedSet only.
PROJECT_CONDITION = re.compile(r"\s*'\$\(MSBuildProjectName\)'\s*==\s*'([A-Za-z0-9_.-]+)'\s*\Z")
INTERVAL = re.compile(r"\[\s*([^,\[\]()\s]+)\s*,\s*([^,\[\]()\s]+)\s*\)\Z")
EXACT_BRACKETS = re.compile(r"\[\s*([^,\[\]()\s]+)\s*\]\Z")


class ToolchainError(RuntimeError):
    """The checkout's toolchain does not fit its release line."""


def stage_of(version: policies.ReleaseVersion) -> str:
    """Return ga, preview or rc for a version; other prerelease labels are not reviewed stages."""
    # WHY: .NET prereleases carry "-preview.[number].[build]" (https://learn.microsoft.com/dotnet/core/versions/) and
    # release candidates rc.N (for example SDK 11.0.100-rc.1.26425.128, EF Core 11.0.0-rc.2 in GHSA-846f-f764-76mx);
    # the three repositories accept only these two stages.
    if not version.is_prerelease:
        return "ga"

    if version.prerelease[0] not in ("preview", "rc"):
        raise ToolchainError("Upstream versions must be GA, preview or rc.")

    return version.prerelease[0]


def matches(name: str, patterns: list[str]) -> bool:
    """Match a package name against exact names and `Prefix.*` patterns, ignoring case like NuGet."""
    # WHY: NuGet package IDs are case-insensitive. https://learn.microsoft.com/nuget/reference/nuspec#id
    lowered = name.lower()
    for pattern in patterns:
        candidate = pattern.lower()
        if candidate.endswith(".*") and lowered.startswith(candidate[:-1]):
            return True

        if lowered == candidate:
            return True

    return False


def _parse(path: Path) -> ElementTree.Element:
    """Parse a regular MSBuild XML file."""
    if path.is_symlink() or not path.is_file():
        raise ToolchainError(f"{path.name} must be a regular file.")

    try:
        return ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as error:
        raise ToolchainError(f"Cannot parse {path.name}.") from error


def _conditions(element: ElementTree.Element, parents: dict) -> list[str]:
    """Return the conditions of an element and all of its ancestors."""
    found, current = [], element
    while current is not None:
        if current.get("Condition"):
            found.append(current.get("Condition"))

        current = parents.get(current)

    return found


def _single_property(root: ElementTree.Element, name: str, label: str) -> str | None:
    """Return the value of a property defined once without a condition, or None when absent."""
    parents = {child: parent for parent in root.iter() for child in parent}
    values = list(root.iter(name))
    if not values:
        return None

    if len(values) != 1 or not (values[0].text or "").strip() or _conditions(values[0], parents):
        raise ToolchainError(f"{label} must define {name} once, without a condition.")

    return values[0].text.strip()


def _default_property(root: ElementTree.Element, name: str, label: str) -> str:
    """Resolve `$(Name)` from one definition in the same file, unconditional or as a default."""
    parents = {child: parent for parent in root.iter() for child in parent}
    values = list(root.iter(name))
    if (len(values) != 1 or not (values[0].text or "").strip()
            or _conditions(values[0], parents) not in ([], [f"'$({name})' == ''"])):
        raise ToolchainError(f"{label} must define {name} once, unconditionally or as a default.")

    return values[0].text.strip()


def bounds(specification: str, major: int, stage: str, label: str) -> tuple[str, str | None]:
    """Validate an exact version or a bounded interval inside one major and return (lower, exclusive upper)."""
    interval = INTERVAL.fullmatch(specification)
    if interval:
        if stage != "ga":
            raise ToolchainError(f"{label}: preview and rc dependencies require an exact version.")

        lower, upper = interval.groups()
        minimum, maximum = policies.parse_version(lower), policies.parse_version(upper)
        if (minimum.major != major or minimum.is_prerelease or maximum.is_prerelease
                or policies.compare_versions(lower, upper) >= 0
                or policies.compare_versions(upper, f"{major + 1}.0.0") > 0):
            raise ToolchainError(f"{label}: the interval must stay inside major {major}.")

        return lower, upper

    exact = EXACT_BRACKETS.fullmatch(specification)
    value = exact[1] if exact else specification
    try:
        parsed = policies.parse_version(value)
    except policies.PolicyError as error:
        raise ToolchainError(f"{label}: {error}") from error

    if parsed.major != major or stage_of(parsed) != stage:
        raise ToolchainError(f"{label}: {value} does not belong to major {major} at stage {stage}.")

    return value, None


def within(version: str, lower: str, upper: str | None) -> bool:
    """Return whether a resolved version satisfies the declared bounds."""
    if upper is None:
        return version == lower

    return policies.compare_versions(version, lower) >= 0 and policies.compare_versions(version, upper) < 0


def check_sdk(root: Path, major: int, stage: str) -> str:
    """Require a fixed SDK of the given major and stage; return the test runner."""
    # WHY: rollForward "disable" requires an exact SDK match; Microsoft recommends it with package lock files
    # "so the SDK version and dependency graph stay in lockstep". allowPrerelease defaults to true outside
    # Visual Studio, so it must be set to match the line's stage. VSTest is the default runner when test.runner
    # is absent. https://learn.microsoft.com/dotnet/core/tools/global-json
    # https://learn.microsoft.com/dotnet/core/tools/dotnet-test
    path = root / "global.json"
    if path.is_symlink() or not path.is_file():
        raise ToolchainError("global.json must be a regular file.")

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ToolchainError("global.json is not valid JSON.") from error

    sdk = document.get("sdk") if isinstance(document, dict) else None
    if not isinstance(sdk, dict) or sdk.get("rollForward") != "disable":
        raise ToolchainError("global.json must set sdk.rollForward to disable.")

    if sdk.get("allowPrerelease") is not (stage != "ga"):
        raise ToolchainError(f"global.json sdk.allowPrerelease must be {str(stage != 'ga').lower()} for stage {stage}.")

    try:
        version = policies.parse_version(sdk.get("version"))
    except policies.PolicyError as error:
        raise ToolchainError(f"global.json sdk.version: {error}") from error

    if version.major != major or stage_of(version) != stage:
        raise ToolchainError(f"global.json selects SDK {sdk.get('version')}, not major {major} at stage {stage}.")

    test = document.get("test", {})
    runner = test.get("runner", "VSTest") if isinstance(test, dict) else None
    if runner not in ("VSTest", "Microsoft.Testing.Platform"):
        raise ToolchainError("global.json test.runner must be VSTest or Microsoft.Testing.Platform.")

    return runner


def target_frameworks(root: Path, coupling: str) -> list[str]:
    """Read the target frameworks from Directory.Build.props."""
    document = _parse(root / "Directory.Build.props")
    single = _single_property(document, "TargetFramework", "Directory.Build.props")
    several = _single_property(document, "TargetFrameworks", "Directory.Build.props")
    if (single is None) == (several is None):
        raise ToolchainError("Directory.Build.props must define either TargetFramework or TargetFrameworks.")

    if coupling == "coupled" and several is not None:
        raise ToolchainError("A coupled line targets exactly one framework (TargetFramework).")

    frameworks = [single] if single is not None else [item.strip() for item in several.split(";") if item.strip()]
    if not frameworks or len(set(frameworks)) != len(frameworks) or any(
            FRAMEWORK.fullmatch(item) is None for item in frameworks):
        raise ToolchainError("Target frameworks must be distinct netN.0 identifiers.")

    return frameworks


def declared_packages(root: Path, patterns: list[str], frameworks: list[str]) -> dict:
    """Return declared versions of framework-coupled packages, keyed by framework, then project scope.

    Scope None holds the repository-wide declaration; a project name holds an
    override under `'$(MSBuildProjectName)' == 'Name'`.
    """
    document = _parse(root / "Directory.Packages.props")
    parents = {child: parent for parent in document.iter() for child in parent}
    declared: dict[str, dict[str | None, dict[str, str]]] = {framework: {} for framework in frameworks}
    for entry in document.iter("PackageVersion"):
        name = entry.get("Include") or entry.get("Update")
        if not isinstance(name, str) or not matches(name, patterns):
            continue

        conditions = _conditions(entry, parents)
        if len(conditions) > 1:
            raise ToolchainError(f"PackageVersion {name} has more than one condition.")

        applies, scope = frameworks, None
        if conditions:
            framework = FRAMEWORK_CONDITION.fullmatch(conditions[0])
            project = PROJECT_CONDITION.fullmatch(conditions[0])
            if framework is not None and framework[1] in frameworks:
                applies = [framework[1]]
            elif project is not None:
                scope = project[1]
            else:
                raise ToolchainError(f"PackageVersion {name} has an unsupported condition.")

        version = entry.get("Version")
        if not isinstance(version, str) or not version.strip():
            raise ToolchainError(f"PackageVersion {name} has no Version.")

        reference = PROPERTY_REFERENCE.fullmatch(version.strip())
        if reference:
            version = _default_property(document, reference[1], "Directory.Packages.props")

        for target in applies:
            packages = declared[target].setdefault(scope, {})
            if name.lower() in packages:
                raise ToolchainError(f"PackageVersion {name} is declared twice for {target}.")

            packages[name.lower()] = version.strip()

    return declared


def project_of(path: Path) -> str:
    """Return the MSBuild project name of the project that owns a lockfile."""
    projects = sorted(path.parent.glob("*.csproj"))
    if len(projects) != 1:
        raise ToolchainError(f"{path.parent.name} must contain exactly one project next to its lockfile.")

    return projects[0].stem


def lockfiles(root: Path, patterns: list[str], expected: int) -> list[Path]:
    """Return the lockfiles matching the policy patterns and require the expected number."""
    found = sorted({path for pattern in patterns for path in root.glob(pattern)})
    if len(found) != expected or any(path.is_symlink() or not path.is_file() for path in found):
        raise ToolchainError(f"Expected {expected} regular lockfiles, found {len(found)}.")

    return found


def check_lockfile(path: Path, root: Path, frameworks: list[str], patterns: list[str],
                   declared: dict[str, dict[str | None, dict[str, tuple[str, str | None]]]], stages: dict[str, str],
                   groups: list[list[str]], observed: dict[tuple[str, str, int], set[str]], *, pinned: bool) -> None:
    """Check the resolved framework-coupled packages of one lockfile."""
    relative = path.relative_to(root).as_posix()
    project = project_of(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ToolchainError(f"{relative} is not valid JSON.") from error

    sections = document.get("dependencies") if isinstance(document, dict) else None
    if not isinstance(sections, dict) or not sections:
        raise ToolchainError(f"{relative} has no dependency sections.")

    for section, packages in sections.items():
        framework = section.split("/", 1)[0]
        if framework not in frameworks or not isinstance(packages, dict):
            raise ToolchainError(f"{relative} has a section for {framework}, which the line does not target.")

        major = int(FRAMEWORK.fullmatch(framework)[1])
        for name, metadata in packages.items():
            if not matches(name, patterns):
                continue

            # WHY: Project references appear in a lockfile with "type": "Project" and no resolved version
            # (SafeMigrations src/*/packages.lock.json).
            if isinstance(metadata, dict) and metadata.get("type") == "Project":
                continue

            resolved = metadata.get("resolved") if isinstance(metadata, dict) else None
            try:
                parsed = policies.parse_version(resolved)
            except policies.PolicyError as error:
                raise ToolchainError(f"{relative}: {name} has no canonical resolved version.") from error

            if parsed.major != major or stage_of(parsed) != stages[framework]:
                raise ToolchainError(f"{relative}: {name} {resolved} does not fit {framework}.")

            # WHY: A central PackageVersion governs direct references, and transitive ones only with transitive
            # pinning ("automatically override a transitive package version ... by opting into ... transitive
            # pinning"). Without it NestedSet.MySql's lockfile records EF as CentralTransitive with requested
            # [10.0.15, ) but resolved 10.0.12.
            # https://learn.microsoft.com/nuget/consume-packages/central-package-management
            scopes = declared[framework]
            limits = scopes.get(project, {}).get(name.lower()) or scopes.get(None, {}).get(name.lower())
            if metadata.get("type") not in (("Direct", "CentralTransitive") if pinned else ("Direct",)):
                limits = None

            if limits is not None and not within(resolved, *limits):
                raise ToolchainError(f"{relative}: {name} {resolved} is outside its declared version.")

            for index, group in enumerate(groups):
                if matches(name, group):
                    observed.setdefault((relative, framework, index), set()).add(resolved)


def check_version_prefix(root: Path, relative: str, major: int) -> None:
    """Require a stable VersionPrefix of the package major."""
    prefix = _single_property(_parse(root / relative), "VersionPrefix", relative)
    if prefix is None:
        raise ToolchainError(f"{relative} must define VersionPrefix.")

    try:
        parsed = policies.parse_version(prefix)
    except policies.PolicyError as error:
        raise ToolchainError(f"{relative} VersionPrefix: {error}") from error

    if parsed.is_prerelease or parsed.major != major:
        raise ToolchainError(f"{relative} VersionPrefix {prefix} must be a stable {major}.x version.")


def transitive_pinning(root: Path) -> bool:
    """Return whether central package management also pins transitive packages."""
    values = set()
    for name in ("Directory.Build.props", "Directory.Packages.props"):
        if (root / name).exists():
            value = _single_property(_parse(root / name), "CentralPackageTransitivePinningEnabled", name)
            if value is not None:
                values.add(value.lower())

    if len(values) > 1 or values - {"true", "false"}:
        raise ToolchainError("CentralPackageTransitivePinningEnabled must be one literal true or false.")

    return values == {"true"}


def check(root: Path, policy: dict, source_ref: str) -> dict:
    """Check the toolchain of a checkout against the line of `source_ref` and return a summary."""
    line = policies.select_line(policy, source_ref, purpose="pr")
    settings = policy["toolchain"]
    coupling, stage, major = settings["coupling"], line["upstreamStage"], line["packageMajor"]
    frameworks = target_frameworks(root, coupling)
    framework_majors = {framework: int(FRAMEWORK.fullmatch(framework)[1]) for framework in frameworks}
    # WHY: EF Core of a major runs only on .NET of the same major ("EF10 requires the .NET 10 SDK to build and
    # requires the .NET 10 runtime to run"), so a coupled provider line binds package, framework and EF major.
    # https://learn.microsoft.com/ef/core/what-is-new/ef-core-10.0/whatsnew
    if coupling == "coupled" and framework_majors[frameworks[0]] != major:
        raise ToolchainError(f"Target framework {frameworks[0]} does not match package major {major}.")

    runner = check_sdk(root, max(framework_majors.values()), stage)
    stages = {framework: stage for framework in frameworks}
    patterns = settings["frameworkPackages"]
    declared = declared_packages(root, patterns, frameworks)
    limits = {framework: {scope: {name: bounds(version, framework_majors[framework], stage, f"{name} for {framework}")
                                  for name, version in packages.items()}
                          for scope, packages in scopes.items()}
              for framework, scopes in declared.items()}
    observed: dict[tuple[str, str, int], set[str]] = {}
    pinned = transitive_pinning(root)
    files = lockfiles(root, settings["lockfiles"], settings["expectedLockfiles"])
    for path in files:
        check_lockfile(path, root, frameworks, patterns, limits, stages, settings["coherentGroups"], observed,
                       pinned=pinned)

    # WHY: Individually valid versions can still mix assemblies of different patches in one project graph
    # (SafeMigrations' rule). It is checked per lockfile: NestedSet's packages resolve EF 10.0.12 and 10.0.13 in
    # different projects, and a consumer's restore unifies them to one version.
    for (relative, framework, index), versions in observed.items():
        if len(versions) != 1:
            raise ToolchainError(f"{relative}: coherent group {index + 1} resolves to several versions for "
                                 f"{framework}: {', '.join(sorted(versions))}.")

    if settings["versionPrefix"] is not None:
        check_version_prefix(root, settings["versionPrefix"], major)

    return {"sourceRef": source_ref, "coupling": coupling, "targetFrameworks": frameworks, "testRunner": runner,
            "lockfiles": len(files)}
