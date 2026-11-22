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
# WHY: bin, obj and artifacts hold build output and copies of project files, as in the audit of
# Doka.EntityFrameworkCore.MySql (eng/quality/audit-dependencies.sh prunes exactly these).
EXCLUDED_DIRECTORIES = {"bin", "obj", "artifacts"}
# WHY: The value of the SafeMigrations audit (eng/release/maintenance.py, run_audit), where a full restore of
# 24 projects with fresh advisory data stays well below it.
RESTORE_TIMEOUT_SECONDS = 1200
LIST_TIMEOUT_SECONDS = 1200


class AuditError(RuntimeError):
    """The audit found a vulnerability or could not prove a complete clean result."""


def source_projects(root: Path) -> list[Path]:
    """Find every project file below the root, ignoring build output and hidden directories."""
    found = []
    for path in root.rglob("*.csproj"):
        parts = path.relative_to(root).parts[:-1]
        # WHY: Hidden directories hold no product projects; the workflow checks this tool out into .workflows.
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
    # WHY: Repositories keep some projects outside their solution on purpose; Doka.EntityFrameworkCore.MySql has
    # 16 example projects that are not in its .slnx but are audited by its own audit.
    entries = "".join(f'  <Project Path="{os.path.relpath(project, path.parent)}" />\n' for project in projects)
    path.write_text(f"<Solution>\n{entries}</Solution>\n", encoding="utf-8")


def write_nuget_config(path: Path) -> None:
    """Write a configuration that restores and audits against nuget.org only."""
    # WHY: Without an explicit auditSources entry NuGet uses the package sources for vulnerability data; a
    # cleared list keeps a repository's extra feeds out of the audit.
    # https://learn.microsoft.com/nuget/concepts/auditing-packages
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

    # WHY: A listing can report zero vulnerable packages after it failed to fetch advisory data; it then
    # reports the failure as a problem. Doka.EntityFrameworkCore.MySql (check-vulnerability-audit.sh) and
    # SafeMigrations (validate_audit) both reject these fields.
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
        # WHY: A clean listing omits the framework collection entirely (SafeMigrations validate_audit).
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
        # WHY: NuGet HTTP-cache file names contain ':', which artifact uploads reject. That failed the audits of
        # MySql, SafeMigrations and NestedSet in October 2026 (for example NestedSet run 41192512161: "The path for
        # one of the files in artifact is not valid: /http-cache/...$ps:_api.nuget.org_v3_index.json/...").
        # https://github.com/actions/toolkit/blob/main/packages/artifact/src/internal/upload/path-and-artifact-name-validation.ts
        require_outside(Path(cache), output)
        environment = {**os.environ, "NUGET_HTTP_CACHE_PATH": cache}
        # WHY: --force and --no-http-cache fetch fresh vulnerability data instead of trusting an earlier restore.
        # NuGetAuditMode=all includes transitive packages (the default only from net10.0 on) and NuGetAuditLevel=low
        # reports every severity. TreatWarningsAsErrors with empty NoWarn and WarningsNotAsErrors makes NU1901-NU1904
        # (vulnerable package) and NU1900/NU1905 (vulnerability data unavailable) fatal, so a failed advisory lookup
        # cannot pass as a clean audit. DokaAuditPipeline=true is the switch NestedSet's Directory.Build.props uses
        # to turn its audit codes into errors; other repositories ignore the property.
        # https://learn.microsoft.com/nuget/concepts/auditing-packages
        restore = ["dotnet", "restore", str(solution), "--force", "--no-http-cache",
                   "--configfile", str(configuration),
                   "-p:NuGetAudit=true", "-p:NuGetAuditMode=all", "-p:NuGetAuditLevel=low",
                   "-p:DokaAuditPipeline=true", "-p:TreatWarningsAsErrors=true", "-p:NoWarn=",
                   "-p:WarningsNotAsErrors="]
        if locked_mode:
            # WHY: The audit must see the committed dependency graph, not a newly resolved one.
            restore.append("--locked-mode")

        if run(restore, cwd=root, environment=environment, stdout=output / "restore.log",
               stderr=output / "restore.err.log", timeout=RESTORE_TIMEOUT_SECONDS):
            raise AuditError("Audited restore failed; see restore.log and restore.err.log.")

        # WHY: --no-restore keeps the audited restore above; from .NET 10 on the command restores by itself
        # otherwise. --output-version 1 is the only allowed JSON version.
        # https://learn.microsoft.com/dotnet/core/tools/dotnet-package-list
        listing = ["dotnet", "package", "list", "--project", str(solution), "--no-restore", "--vulnerable",
                   "--include-transitive", "--format", "json", "--output-version", "1",
                   "--config", str(configuration)]
        if run(listing, cwd=root, environment=environment, stdout=output / "audit.json",
               stderr=output / "audit.log", timeout=LIST_TIMEOUT_SECONDS):
            raise AuditError("Vulnerability listing failed; see audit.log.")

        # WHY: SafeMigrations rejects any listing diagnostics; a warning there means the result is incomplete.
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
