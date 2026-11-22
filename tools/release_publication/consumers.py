"""Isolated package consumers: restore exact candidate bytes, verify them, and derive one SBOM per package.

Ported from NestedSet's eng/verify-package-sbom.py and generalized to any
candidate package set. A consumer project references exactly one package at
its exact version; its restore must resolve the candidate's own bytes (local
feed) or, after publication, the bytes on nuget.org.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from urllib.parse import unquote

from tools.release_publication import packages

NUGET_SOURCE = "https://api.nuget.org/v3/index.json"
MANIFEST_PATH = Path("_manifest/spdx_2.2/manifest.spdx.json")
# WHY: Test and build tooling must never appear in the dependency graph of a shipped package (NestedSet's rule).
FORBIDDEN_PREFIXES = ("xunit", "nunit", "mstest", "testcontainers", "coverlet", "benchmarkdotnet")
FORBIDDEN_PACKAGES = {"microsoft.net.test.sdk", "microsoft.sbom.dotnettool", "sbom.tool"}
# WHY: Producer settings in the environment can redirect restore output or repair the package graph; the consumer
# restores only its candidate (NestedSet's list).
REDIRECTING_VARIABLES = ("ArtifactsPath", "UseArtifactsOutput", "BaseIntermediateOutputPath", "BaseOutputPath",
                         "MSBuildProjectExtensionsPath", "DirectoryPackagesPropsPath", "RestorePackagesPath",
                         "NuGetLockFilePath", "RestoreLockedMode")


class ConsumerError(RuntimeError):
    """A consumer restore does not reproduce the candidate or its SBOM does not describe it."""


def require(condition: bool, message: str) -> None:
    """Raise a consumer error when a condition does not hold."""
    if not condition:
        raise ConsumerError(message)


def frameworks_of(archive: Path) -> list[str]:
    """Return the target frameworks of a package from its nuspec dependency groups."""
    record = packages.nuspec(packages.archive_entries(archive))
    groups = [group.get("targetFramework") for group in record.findall("{*}dependencies/{*}group")]
    require(bool(groups) and all(groups), f"{archive.name} declares no target framework groups.")
    return sorted(groups)


def declared_dependencies(archive: Path, framework: str) -> dict[str, str]:
    """Return the dependencies the package declares for one target framework."""
    record = packages.nuspec(packages.archive_entries(archive))
    for group in record.findall("{*}dependencies/{*}group"):
        if group.get("targetFramework") == framework:
            items = group.findall("{*}dependency")
            result = {item.get("id").lower(): item.get("version") for item in items}
            require(len(result) == len(items), f"{archive.name} declares a dependency twice.")
            return result

    raise ConsumerError(f"{archive.name} has no dependency group for {framework}.")


def run_logged(arguments: list[str], log: Path, cwd: Path, environment: dict | None = None) -> None:
    """Run a native tool with its output kept in a log file."""
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(arguments, cwd=cwd, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                timeout=1200, check=False)

    require(result.returncode == 0, f"Command failed ({result.returncode}); see {log}")


def restore_consumer(work: Path, package_id: str, version: str, frameworks: list[str], sdk: Path,
                     feed: Path | None, local_ids: list[str], log: Path) -> Path:
    """Restore a fresh consumer of one exact package; with a feed, candidate packages come only from that feed."""
    consumer = work / "consumer"
    consumer.mkdir(parents=True)
    shutil.copyfile(sdk, consumer / "global.json")
    project = ElementTree.Element("Project", {"Sdk": "Microsoft.NET.Sdk"})
    properties = ElementTree.SubElement(project, "PropertyGroup")
    # WHY: NuGetAudit=false: a newly published advisory must not fail a release; vulnerabilities are the scheduled
    # dependency audit's and the pull request review's job. https://learn.microsoft.com/nuget/concepts/auditing-packages
    for key, value in {"TargetFrameworks": ";".join(frameworks), "NuGetAudit": "false"}.items():
        ElementTree.SubElement(properties, key).text = value

    ElementTree.SubElement(ElementTree.SubElement(project, "ItemGroup"), "PackageReference",
                           {"Include": package_id, "Version": f"[{version}]"})
    ElementTree.ElementTree(project).write(consumer / "Consumer.csproj", encoding="utf-8", xml_declaration=True)
    config = ElementTree.Element("configuration")
    sources = ElementTree.SubElement(config, "packageSources")
    ElementTree.SubElement(sources, "clear")
    if feed is not None:
        ElementTree.SubElement(sources, "add", {"key": "local", "value": str(feed)})

    ElementTree.SubElement(sources, "add", {"key": "nuget", "value": NUGET_SOURCE})
    if feed is not None:
        # WHY: Package source mapping makes the candidate packages resolvable only from the local feed, so a public
        # package of the same identity can never stand in for the candidate.
        # https://learn.microsoft.com/nuget/consume-packages/package-source-mapping
        mapping = ElementTree.SubElement(config, "packageSourceMapping")
        local = ElementTree.SubElement(mapping, "packageSource", {"key": "local"})
        for name in local_ids:
            ElementTree.SubElement(local, "package", {"pattern": name})

        ElementTree.SubElement(ElementTree.SubElement(mapping, "packageSource", {"key": "nuget"}), "package",
                               {"pattern": "*"})

    ElementTree.ElementTree(config).write(consumer / "NuGet.Config", encoding="utf-8", xml_declaration=True)
    environment = {key: value for key, value in os.environ.items() if key not in REDIRECTING_VARIABLES}
    run_logged(["dotnet", "restore", "Consumer.csproj", "--configfile", "NuGet.Config", "--packages",
                str(work / "packages"), "--disable-build-servers", "-p:ImportDirectoryBuildProps=false",
                "-p:ImportDirectoryBuildTargets=false", "-p:ManagePackageVersionsCentrally=false"], log, consumer,
               environment)
    return consumer


def closure(assets: dict, package_id: str, version: str, frameworks: list[str]) -> set[tuple[str, str]]:
    """Return the resolved package closure of the consumer and require it to hold nothing else."""
    require(sorted(assets["targets"]) == sorted(frameworks), "The consumer resolved other target frameworks.")
    for framework in frameworks:
        dependencies = assets["project"]["frameworks"][framework]["dependencies"]
        require(set(dependencies) == {package_id} and dependencies[package_id]["version"] == f"[{version}, {version}]",
                "The consumer must reference exactly its package at its exact version.")
        require(not assets["project"]["restore"]["frameworks"][framework]["projectReferences"],
                "The consumer must not contain project references.")

    libraries = assets["libraries"]
    resolved = set()
    # WHY: Each target framework resolves its own graph, so one dependency can appear in several versions, one per
    # framework (a package declaring other dependency versions for net10.0 and net11.0).
    for identity, library in libraries.items():
        require(library["type"] == "package", f"The consumer resolved a non-package component: {identity}.")
        name, package_version = identity.rsplit("/", 1)
        name = name.lower()
        require(name not in FORBIDDEN_PACKAGES and not name.startswith(FORBIDDEN_PREFIXES),
                f"Test or build tooling leaked into the shipped dependency graph: {name}.")
        resolved.add((name, package_version))

    require((package_id.lower(), version) in resolved, f"{package_id} {version} is missing from its own closure.")
    require(sum(name == package_id.lower() for name, _ in resolved) == 1,
            f"The consumer resolved several versions of {package_id}.")
    return resolved


def require_candidate_bytes(assets: dict, cache: Path, archives: dict[str, Path], version: str) -> None:
    """Require every restored candidate package to be byte-identical to the candidate archive (NestedSet)."""
    for name, archive in archives.items():
        identity = next((key for key in assets["libraries"] if key.rsplit("/", 1)[0].lower() == name.lower()), None)
        if identity is None:
            continue

        require(identity.rsplit("/", 1)[1] == version, f"The consumer restored another version of {name}.")
        with archive.open("rb") as stream:
            expected = base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode("ascii")

        require(assets["libraries"][identity]["sha512"] == expected, f"The restored {name} has other bytes.")
        restored = list((cache / assets["libraries"][identity]["path"]).glob("*.nupkg"))
        require(len(restored) == 1 and packages.sha256(restored[0]) == packages.sha256(archive),
                f"The restored archive of {name} differs from the candidate.")


def verify_manifest(manifest: dict, archive: Path, resolved: set[tuple[str, str]], package_id: str,
                    version: str) -> None:
    """Require the SPDX document to describe exactly the archive and exactly the resolved closure (NestedSet)."""
    require(manifest.get("spdxVersion") == "SPDX-2.2", "Expected an SPDX 2.2 document.")
    roots = [item for item in manifest["packages"] if item["SPDXID"] == "SPDXRef-RootPackage"]
    require(len(roots) == 1 and (roots[0]["name"], roots[0]["versionInfo"]) == (package_id, version)
            and manifest.get("documentDescribes") == ["SPDXRef-RootPackage"], "The SBOM describes another root.")
    files = manifest["files"]
    require(len(files) == 1 and files[0]["fileName"] == f"./{archive.name}"
            and [item["checksumValue"].lower() for item in files[0]["checksums"] if item["algorithm"] == "SHA256"]
            == [packages.sha256(archive)], "The SBOM does not describe exactly the candidate archive.")
    actual = set()
    for item in manifest["packages"]:
        if item is roots[0]:
            continue

        identity = (item["name"].lower(), item["versionInfo"])
        references = [unquote(ref["referenceLocator"]).lower() for ref in item.get("externalRefs", [])
                      if ref["referenceType"] == "purl"]
        require(identity not in actual and references == [f"pkg:nuget/{item['name']}@{item['versionInfo']}".lower()],
                f"The SBOM component {identity} is repeated or has another package URL.")
        actual.add(identity)

    require(actual == resolved, f"The SBOM components differ from the closure: missing {sorted(resolved - actual)}, "
                                f"extra {sorted(actual - resolved)}.")


def verify_validation(report: dict) -> None:
    """Require sbom-tool to have validated exactly one unmodified file, not only to have exited with 0."""
    # WHY: The field is spelled "ValidationTelemetery" in sbom-tool's report (NestedSet's verify_vendor).
    telemetry = report["Summary"]["ValidationTelemetery"]
    require(report["Result"] == "Success" and report["ValidationErrors"]["Count"] == 0
            and telemetry["TotalFilesInManifest"] == 1 and telemetry["FilesSuccessfulCount"] == 1
            and telemetry["FilesValidatedCount"] == 1 and telemetry["FilesFailedCount"] == 0
            and telemetry["FilesSkippedCount"] == 0, "sbom-tool did not validate exactly the candidate archive.")


def generate(directory: Path, inventory: list[dict], version: str, tool: Path, sdk: Path, repository: str,
             supplier: str, output: Path) -> list[dict]:
    """Restore an isolated consumer per package from the candidate feed and write a validated SBOM for it."""
    archives = {item["id"]: (directory / item["nupkg"]).resolve() for item in inventory}
    output.mkdir(parents=True, exist_ok=True)
    summaries = []
    with tempfile.TemporaryDirectory(prefix="candidate-consumers-") as temporary:
        work = Path(temporary)
        feed = work / "feed"
        feed.mkdir()
        for archive in archives.values():
            shutil.copyfile(archive, feed / archive.name)

        for package_id, archive in archives.items():
            package_work = work / package_id
            drop = package_work / "drop"
            drop.mkdir(parents=True)
            shutil.copyfile(archive, drop / archive.name)
            frameworks = frameworks_of(archive)
            consumer = restore_consumer(package_work, package_id, version, frameworks, sdk, feed, list(archives),
                                        output / f"{package_id}.restore.log")
            assets = json.loads((consumer / "obj/project.assets.json").read_text(encoding="utf-8"))
            resolved = closure(assets, package_id, version, frameworks)
            require_candidate_bytes(assets, package_work / "packages", archives, version)
            # WHY: The documented sbom-tool arguments: -b drop with the archive, -bc the consumer whose restore
            # holds the components, -mi SPDX:2.2, -n fails validation without packages.
            # https://github.com/microsoft/sbom-tool/blob/v4.1.5/docs/sbom-tool-arguments.md
            run_logged([str(tool), "generate", "-b", str(drop), "-bc", str(consumer), "-m", str(package_work),
                        "-pn", package_id, "-pv", version, "-ps", supplier,
                        "-nsb", f"https://github.com/{repository}/sbom", "-mi", "SPDX:2.2"],
                       output / f"{package_id}.generation.log", package_work)
            run_logged([str(tool), "validate", "-b", str(drop), "-m", str(package_work / "_manifest"),
                        "-o", str(package_work / "validation.json"), "-mi", "SPDX:2.2", "-n"],
                       output / f"{package_id}.validation.log", package_work)
            verify_validation(json.loads((package_work / "validation.json").read_text(encoding="utf-8")))
            manifest = json.loads((package_work / MANIFEST_PATH).read_text(encoding="utf-8"))
            verify_manifest(manifest, drop / archive.name, resolved, package_id, version)
            shutil.copyfile(package_work / MANIFEST_PATH, directory / f"{package_id}.spdx.json")
            summaries.append({"package": package_id, "components": len(resolved) - 1})

    return summaries


def verify_public(directory: Path, inventory: list[dict], version: str, sdk: Path, output: Path) -> list[dict]:
    """Restore each published package from nuget.org only and require the candidate's payload (MySql)."""
    output.mkdir(parents=True, exist_ok=True)
    result = []
    with tempfile.TemporaryDirectory(prefix="public-consumers-") as temporary:
        for item in inventory:
            archive = directory / item["nupkg"]
            work = Path(temporary) / item["id"]
            consumer = restore_consumer(work, item["id"], version, frameworks_of(archive), sdk, None, [],
                                        output / f"{item['id']}.public-restore.log")
            assets = json.loads((consumer / "obj/project.assets.json").read_text(encoding="utf-8"))
            identity = next(key for key in assets["libraries"] if key.rsplit("/", 1)[0].lower() == item["id"].lower())
            restored = list((work / "packages" / assets["libraries"][identity]["path"]).glob("*.nupkg"))
            # WHY: nuget.org adds its repository signature, so the public bytes differ only by .signature.p7s.
            require(len(restored) == 1 and packages.canonical_payload(restored[0]) == packages.canonical_payload(archive),
                    f"nuget.org serves another payload for {item['id']} {version}.")
            result.append({"package": item["id"], "restoredFrom": NUGET_SOURCE})

    return result
