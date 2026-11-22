"""Audit every .NET project of a repository for known direct and transitive vulnerabilities.

The audit restores all projects with NuGet audit warnings as errors, lists
vulnerable packages as JSON and validates that document before it accepts a
clean result. An exit code of zero from the .NET CLI alone is not accepted as
evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

NUGET_SOURCE = "https://api.nuget.org/v3/index.json"
EXCLUDED_DIRECTORIES = {"bin", "obj", "artifacts", "node_modules"}
RESTORE_TIMEOUT_SECONDS = 1200
LIST_TIMEOUT_SECONDS = 1200


class AuditError(RuntimeError):
    """The audit found a vulnerability or could not prove a complete clean result."""


def source_projects(root: Path) -> list[Path]:
    """Find every project file below the root, ignoring build output and hidden directories."""
    found = []
    for path in root.rglob("*.csproj"):
        parts = path.relative_to(root).parts[:-1]
        if any(part in EXCLUDED_DIRECTORIES or part.startswith(".") for part in parts):
            continue

        if path.is_symlink() or not path.is_file():
            raise AuditError(f"Project file is not a regular file: {path.relative_to(root)}")

        found.append(path.resolve())

    if not found:
        raise AuditError("The repository contains no project file.")

    return sorted(found)


def write_audit_solution(path: Path, projects: list[Path]) -> None:
    """Write a solution that lists every project, independent of the repository's own solutions."""
    # WHY: Repositories keep some projects, such as examples, outside their solution on purpose;
    # the audit covers every project file regardless.
    entries = "".join(f'  <Project Path="{os.path.relpath(project, path.parent)}" />\n' for project in projects)
    path.write_text(f"<Solution>\n{entries}</Solution>\n", encoding="utf-8")


def write_nuget_config(path: Path) -> None:
    """Write a configuration that restores and audits against nuget.org only."""
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<configuration>\n"
        f'  <packageSources><clear /><add key="nuget.org" value="{NUGET_SOURCE}" /></packageSources>\n'
        f'  <auditSources><clear /><add key="nuget.org" value="{NUGET_SOURCE}" /></auditSources>\n'
        "</configuration>\n",
        encoding="ascii")


def require_outside(path: Path, evidence: Path) -> None:
    """Keep disposable files out of the evidence directory, including through symbolic links."""
    resolved = path.resolve()
    if resolved == evidence.resolve() or evidence.resolve() in resolved.parents:
        raise AuditError("The NuGet HTTP cache must be outside the audit evidence directory.")


def validate_report(report: object, expected: list[Path]) -> int:
    """Validate the vulnerability listing and return the number of audited projects."""
    if not isinstance(report, dict) or type(report.get("version")) is not int or report["version"] != 1:
        raise AuditError("Audit document does not declare schema version 1.")

    parameters = report.get("parameters")
    if not isinstance(parameters, str) or any(
            flag not in parameters.split() for flag in ("--vulnerable", "--include-transitive")):
        raise AuditError("Audit document was not produced with --vulnerable --include-transitive.")

    if report.get("sources") != [NUGET_SOURCE]:
        raise AuditError("Audit document does not use nuget.org as its only source.")

    for key in ("problems", "warnings", "errors"):
        if report.get(key, []) != []:
            raise AuditError(f"Audit document reports {key}; that is not a clean result.")

    projects = report.get("projects")
    if not isinstance(projects, list) or not projects:
        raise AuditError("Audit document contains no project inventory.")

    observed = []
    for project in projects:
        if not isinstance(project, dict) or not isinstance(project.get("path"), str) or not project["path"]:
            raise AuditError("Audit document contains a project without a path.")

        for key in ("problems", "warnings", "errors"):
            if project.get(key, []) != []:
                raise AuditError(f"Audit of {project['path']} reports {key}.")

        observed.append(Path(project["path"]).resolve())
        # WHY: A clean listing omits the framework collection entirely.
        frameworks = project.get("frameworks", [])
        if not isinstance(frameworks, list):
            raise AuditError(f"Framework inventory of {project['path']} is malformed.")

        seen_frameworks = set()
        for framework in frameworks:
            name = framework.get("framework") if isinstance(framework, dict) else None
            if not isinstance(name, str) or not name or name in seen_frameworks:
                raise AuditError(f"Framework of {project['path']} is missing or repeated.")

            seen_frameworks.add(name)
            for kind in ("topLevelPackages", "transitivePackages"):
                packages = framework.get(kind, [])
                if not isinstance(packages, list):
                    raise AuditError(f"{kind} of {project['path']} is malformed.")

                seen_packages = set()
                for package in packages:
                    if (not isinstance(package, dict) or not isinstance(package.get("id"), str)
                            or not package["id"] or package["id"] in seen_packages
                            or not isinstance(package.get("resolvedVersion"), str)
                            or not package["resolvedVersion"]
                            or not isinstance(package.get("vulnerabilities"), list)):
                        raise AuditError(f"Package evidence of {project['path']} is incomplete or repeated.")

                    seen_packages.add(package["id"])
                    if package["vulnerabilities"]:
                        raise AuditError(
                            f"{package['id']} {package['resolvedVersion']} in {project['path']} has known "
                            f"vulnerabilities ({len(package['vulnerabilities'])}).")

    if len(observed) != len(set(observed)) or set(observed) != set(expected):
        raise AuditError("Audit document does not cover exactly the project files of the repository.")

    return len(observed)


def run(command: list[str], *, cwd: Path, environment: dict, stdout: Path, stderr: Path, timeout: int) -> int:
    """Run one .NET CLI command with its output retained as evidence."""
    with stdout.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
        return subprocess.run(command, cwd=cwd, env=environment, stdout=out, stderr=err,
                              check=False, timeout=timeout).returncode


def audit(root: Path, output: Path, *, locked_mode: bool, cache_parent: Path) -> int:
    """Run the complete audit and return the number of audited projects."""
    expected = source_projects(root)
    output.mkdir(parents=True, exist_ok=True)
    solution = output / "audit.slnx"
    write_audit_solution(solution, expected)
    configuration = output / "NuGet.Config"
    write_nuget_config(configuration)
    with tempfile.TemporaryDirectory(prefix="nuget-audit-http-", dir=cache_parent) as cache:
        # WHY: NuGet HTTP-cache file names contain ':', which artifact uploads reject.
        require_outside(Path(cache), output)
        environment = {**os.environ, "NUGET_HTTP_CACHE_PATH": cache}
        restore = ["dotnet", "restore", str(solution), "--force", "--no-http-cache", "--disable-build-servers",
                   "--configfile", str(configuration),
                   "-p:NuGetAudit=true", "-p:NuGetAuditMode=all", "-p:NuGetAuditLevel=low",
                   "-p:DokaAuditPipeline=true", "-p:TreatWarningsAsErrors=true", "-p:NoWarn=",
                   "-p:WarningsNotAsErrors="]
        if locked_mode:
            restore.append("--locked-mode")

        if run(restore, cwd=root, environment=environment, stdout=output / "restore.log",
               stderr=output / "restore.err.log", timeout=RESTORE_TIMEOUT_SECONDS):
            raise AuditError("Audited restore failed; see restore.log and restore.err.log.")

        listing = ["dotnet", "package", "list", "--project", str(solution), "--no-restore", "--vulnerable",
                   "--include-transitive", "--format", "json", "--output-version", "1",
                   "--config", str(configuration)]
        if run(listing, cwd=root, environment=environment, stdout=output / "audit.json",
               stderr=output / "audit.log", timeout=LIST_TIMEOUT_SECONDS):
            raise AuditError("Vulnerability listing failed; see audit.log.")

        if (output / "audit.log").read_text(encoding="utf-8").strip():
            raise AuditError("Vulnerability listing wrote diagnostics; see audit.log.")

    try:
        report = json.loads((output / "audit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AuditError("Vulnerability listing is not valid JSON.") from error

    return validate_report(report, expected)


def write_summary(output: Path, result: str, detail: str, project_count: int) -> None:
    """Write the machine-readable summary and, on GitHub, the job summary."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(
        {"schemaVersion": 1, "result": result, "projectCount": project_count, "detail": detail},
        indent=2) + "\n", encoding="utf-8")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as stream:
            stream.write(f"## Dependency audit\n\nResult: **{result}**; projects: {project_count}.\n\n{detail}\n")


def main(argv: list[str] | None = None) -> int:
    """Run the audit from the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-locked-mode", action="store_true")
    parser.add_argument("--cache-parent", type=Path, default=Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output = args.output.resolve()
    try:
        count = audit(root, output, locked_mode=not args.no_locked_mode, cache_parent=args.cache_parent)
    except (AuditError, subprocess.TimeoutExpired) as error:
        write_summary(output, "failed", str(error), 0)
        print(f"Dependency audit failed: {error}", file=sys.stderr)
        return 1

    write_summary(output, "passed", "No known direct or transitive vulnerabilities.", count)
    print(f"Dependency audit passed: {count} projects.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
