"""Qualification of a pull request and of a release candidate.

A pull request attests its tested merge tree once its gate jobs succeeded. A
release candidate is qualified when its commit's tree carries such a pull
request attestation, the candidate's own gate jobs succeeded in the release
run, and its test results are complete.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from tools.change_scope import scope as change_scope
from tools.dependency_audit.audit import source_projects
from tools.release_lines import attestation
from tools.release_lines import policy as policies
from tools.release_lines.github import GitHubError

# WHY: A bound against an endless walk; each step is one merged documentation-only pull request.
MAX_DOCUMENTATION_CHAIN = 50
TRX_FAILURES = ("failed", "error", "timeout", "aborted", "passedButRunAborted", "notRunnable", "disconnected")
TRX_FAILED_OUTCOMES = ("Failed", "Error", "Timeout", "Aborted")
TRX_NAMESPACE = "{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}"
# WHY: The VSTest adapter and the testing platform write a skip reason to different elements of the same TRX
# schema, and the schema has no public specification. Measured on SDK 10.0.404 (files in tests/data):
# xunit.runner.visualstudio 4.0.1 under VSTest ("dotnet test --logger trx") names the adapter
# executor://xunit/VsTestRunner3/<platform>/ for xUnit v3 4.0.3 and xUnit v2 2.9.3 alike and writes
# Output/ErrorInfo/Message; the xUnit report under the Microsoft Testing Platform ("dotnet test --report-xunit-trx")
# names executor://<run id>/xunit.v3/<version> and writes Output/StdOut. MySql runs both, VSTest on maintenance/10.x
# and the testing platform on main; SafeMigrations runs xUnit v2 under VSTest. A runner whose layout was not
# measured is refused instead of read from a guessed element.
# https://github.com/xunit/visualstudio.xunit
# https://xunit.net/docs/getting-started/v3/microsoft-testing-platform
TRX_SKIP_REASONS = (
    (re.compile(r"executor://xunit/VsTestRunner3/"), ("Output", "ErrorInfo", "Message")),
    (re.compile(r"executor://[0-9a-f-]{36}/xunit\.v3/"), ("Output", "StdOut")),
)
WORKFLOW_REF = re.compile(r"([^/]+/[^/]+)/(\.github/workflows/[^@]+)@(.+)\Z")
# WHY: An MSBuild configuration name as dotnet pack --configuration takes it; anything else could inject properties.
CONFIGURATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class QualificationError(RuntimeError):
    """A gate, a test result or an attestation does not qualify the candidate."""


def git_tree(root: Path, commit: str) -> str:
    """Return the tree ID of a commit in a checkout."""
    completed = subprocess.run(["git", "-C", str(root), "rev-parse", f"{commit}^{{tree}}"], capture_output=True,
                               text=True, timeout=60, check=False)
    tree = completed.stdout.strip()
    if completed.returncode or not attestation.TREE.fullmatch(tree):
        raise QualificationError(f"Cannot read the tree of {commit}.")

    return tree


def workflow_path(environment: dict) -> str:
    """Return the workflow file path of the running (calling) workflow."""
    match = WORKFLOW_REF.fullmatch(environment.get("GITHUB_WORKFLOW_REF", ""))
    if match is None or match[1] != environment.get("GITHUB_REPOSITORY"):
        raise QualificationError("GITHUB_WORKFLOW_REF does not name a workflow of this repository.")

    return match[2]


def run_jobs(api, repository: str, run_id: str) -> list[dict]:
    """List the jobs of the latest attempt of a run."""
    jobs = []
    for page in range(1, 102):
        response = api.request(f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}")
        entries = response.get("jobs") if isinstance(response, dict) else None
        if not isinstance(entries, list):
            raise QualificationError("The job listing of this run is invalid.")

        jobs.extend(entries)
        if len(entries) < 100:
            return jobs

    raise QualificationError("The job listing of this run exceeded its page bound.")


def require_gate_jobs(jobs: list[dict], names: list[str]) -> list[dict]:
    """Require every gate job, including all of its matrix rows, to have succeeded in this run."""
    # WHY: GitHub names a job of a called workflow "<caller job> / <job>" and a matrix row "<job> (<values>)", as the
    # job listings show (NestedSet run 44952104737 "Qualification / SQLite integration tests", MySql
    # "qualification / spec-test-suite (mysql84)"); the documentation does not describe the format. Skipped is not
    # success (MySql's selection rule).
    result = []
    for name in names:
        matches = [job for job in jobs if job.get("name") == name or str(job.get("name", "")).startswith(f"{name} (")]
        if not matches or any(job.get("status") != "completed" or job.get("conclusion") != "success"
                              for job in matches):
            raise QualificationError(f"Gate job '{name}' is missing, skipped or unsuccessful in this run.")

        result.append({"name": name, "jobs": sorted(job["name"] for job in matches), "conclusion": "success"})

    return result


def pr_predicate(api, policy: dict, environment: dict, event: dict, root: Path) -> tuple[str, dict]:
    """Return the tested merge tree and the predicate of a pull request qualification."""
    if environment.get("GITHUB_EVENT_NAME") != "pull_request":
        raise QualificationError("A pull request qualification is created only for pull_request runs.")

    pull = event.get("pull_request", {})
    merge = environment.get("GITHUB_SHA", "")
    # WHY: For pull_request, GITHUB_SHA is "the SHA of the merge commit on the merge branch"; its tree is what
    # the run tested.
    # https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request
    if git_tree(root, "HEAD") != git_tree(root, merge):
        raise QualificationError("The checkout is not the merge commit this run tested.")

    base = f"refs/heads/{pull.get('base', {}).get('ref', '')}"
    policies.check_pr_base(policy, base)
    path = workflow_path(environment)
    names = [gate["name"] for gate in policy["qualification"]["gates"]
             if gate["kind"] == "protected-check" and gate["workflow"] == path]
    if not names:
        raise QualificationError(f"The release policy declares no protected-check gate for {path}.")

    gates = require_gate_jobs(run_jobs(api, policy["repository"], environment["GITHUB_RUN_ID"]), names)
    tree = git_tree(root, merge)
    base_sha = pull.get("base", {}).get("sha", "")
    # WHY: The scope is computed here from the tested merge commit and its base, not taken from the caller, so a
    # documentation-only attestation always means a change the release qualification can recompute.
    try:
        scope = change_scope.classify(root, base_sha, merge)["scope"]
    except change_scope.ScopeError as error:
        raise QualificationError(f"Cannot classify the pull request change: {error}") from error

    return tree, {"schemaVersion": 1, "repository": policy["repository"], "pullRequest": pull.get("number"),
                  "baseRef": base, "baseSha": base_sha, "baseTree": git_tree(root, base_sha),
                  "headSha": pull.get("head", {}).get("sha"), "mergeCommit": merge, "tree": tree,
                  "workflow": path, "scope": scope, "gates": gates}


def restore_projects(root: Path) -> None:
    """Restore the locked dependency graph of the projects used for qualification."""
    # WHY: Restore must use the same inventory as MSBuild property evaluation. A separate shell scan restored
    # projects in nested hidden directories even though the evaluation excludes them. Locked mode prevents changes
    # to the lockfile. NuGetAudit stays disabled here because dependency audit and review are separate checks.
    # https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-restore
    # https://learn.microsoft.com/en-us/nuget/concepts/auditing-packages
    for project in source_projects(root):
        command = ["dotnet", "restore", str(project), "--locked-mode", "-p:NuGetAudit=false"]
        try:
            completed = subprocess.run(command, cwd=root, check=False)
        except OSError as error:
            raise QualificationError(f"Cannot start restore of {project.relative_to(root)}.") from error

        if completed.returncode:
            raise QualificationError(f"Cannot restore {project.relative_to(root)} in locked mode.")


def test_projects(root: Path) -> dict[str, str]:
    """Return the test projects of a restored checkout as assembly name to relative project path."""
    # WHY: Whether a project is a test project is decided by MSBuild evaluation (IsTestProject=true, set for example
    # by Microsoft.NET.Test.Sdk.props), not by the project XML: MySql has helper and smoke projects without the
    # property, and NestedSet's shared Specification.Tests library evaluates to false.
    result = {}
    for project in source_projects(root):
        properties = evaluate(root, project, ["IsTestProject", "AssemblyName"])
        if properties.get("IsTestProject", "").lower() == "true":
            name = properties.get("AssemblyName") or project.stem
            if name in result:
                raise QualificationError(f"Two test projects build the assembly {name}.")

            result[name] = project.relative_to(root).as_posix()

    return result


def evaluate(root: Path, project: Path, names: list[str], settings: dict[str, str] | None = None) -> dict[str, str]:
    """Return MSBuild-evaluated properties of a restored project, optionally with global properties set."""
    from tools.release_lines.package_validation import property_arguments

    command = ["dotnet", "msbuild", str(project), "-nologo", *(f"-getProperty:{name}" for name in names),
               *property_arguments(settings or {})]
    # WHY: The CLI "looks for a global.json file iteratively navigating the path upward from the current working
    # directory"; the job runs in the checkout of this repository, whose global.json pins another SDK.
    # https://learn.microsoft.com/dotnet/core/tools/global-json
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=300, check=False)
    try:
        properties = json.loads(completed.stdout)["Properties"]
    except (ValueError, KeyError, TypeError) as error:
        raise QualificationError(f"MSBuild cannot evaluate {project.relative_to(root)}.") from error

    if completed.returncode:
        raise QualificationError(f"MSBuild cannot evaluate {project.relative_to(root)}.")

    return properties


def require_package_validation(root: Path, package_ids: list[str], *, package_dir: Path, version: str,
                               configuration: str, work: Path, run=subprocess.run, fetcher=None,
                               verifier=None) -> list[dict]:
    """Require each release package to come from a project that validates it, and validate its exact archive."""
    # WHY: "EnablePackageValidation runs a series of checks after the Pack task"; MySql, SafeMigrations and
    # NestedSet enable it for every shipped project, and only NestedSet sets a baseline version. The property is
    # read in the pack's configuration and version, because a project can set it for one configuration only.
    # https://learn.microsoft.com/dotnet/fundamentals/apicompat/package-validation/overview
    # WHY: Microsoft offers the same validation "as an MSBuild task or using the Microsoft.DotNet.ApiCompat.Tool
    # global tool"; the tool checks the archive the candidate ships, independent of how the pack job ran.
    # https://learn.microsoft.com/dotnet/fundamentals/apicompat/global-tool
    from tools.release_lines import package_validation as validation
    from tools.release_publication import nuget, packages

    fetcher = fetcher or nuget.fetch
    verifier = verifier or nuget.verify_signature
    if not CONFIGURATION.fullmatch(configuration):
        raise QualificationError("The pack configuration must be an MSBuild configuration name.")

    policies.parse_version(version)
    settings = {"Configuration": configuration, "Version": version, "PackageVersion": version}
    expected = {f"{name}.{version}.nupkg" for name in package_ids}
    if {path.name for path in package_dir.glob("*.nupkg")} != expected:
        raise QualificationError("The package artifact does not hold exactly the release packages of this version.")

    found = {}
    for project in source_projects(root):
        properties = evaluate(root, project, ["PackageId", "IsPackable", "EnablePackageValidation"], settings)
        if properties.get("IsPackable", "").lower() != "true" or properties.get("PackageId") not in package_ids:
            continue

        if properties["PackageId"] in found:
            raise QualificationError(f"Two projects pack {properties['PackageId']}.")

        if properties.get("EnablePackageValidation", "").lower() != "true":
            raise QualificationError(f"{project.relative_to(root)} packs {properties['PackageId']} without "
                                     f"EnablePackageValidation in {configuration}.")

        found[properties["PackageId"]] = project

    missing = sorted(set(package_ids) - set(found))
    if missing:
        raise QualificationError(f"No packable project produces {', '.join(missing)}.")

    try:
        tool = validation.install_tool(root, work / "tool", run=run, fetcher=fetcher, verifier=verifier)
        result = []
        for name, project in sorted(found.items()):
            # WHY: The SDK validates with the references of each inner build, which exist only after a build; the
            # pack builds the same way before it validates. --no-restore keeps the locked graph restored above.
            # https://learn.microsoft.com/dotnet/core/tools/dotnet-build
            completed = run(["dotnet", "build", str(project), "--no-restore",
                             *validation.property_arguments(settings)], cwd=root, check=False)
            if completed.returncode:
                raise QualificationError(f"Cannot build {project.relative_to(root)} in {configuration}.")

            package = package_dir / f"{name}.{version}.nupkg"
            if package.is_symlink() or not package.is_file():
                raise QualificationError(f"The release package {package.name} must be a regular file.")

            digest = packages.sha256(package)
            state = validation.collect(root, project, settings, run=run)
            references = validation.references(root, project, settings, state, run=run)
            baseline = validation.baseline(state, work / "baseline" / name, root, fetcher=fetcher, verifier=verifier)
            # WHY: The tool targets net8.0 with rollForward Major and so starts on the lowest major runtime from 8 on,
            # while the SDK's Roslyn that the pack used, which CollectApiCompatInputs names, needs the SDK's own
            # runtime. DOTNET_ROLL_FORWARD overrides runtimeconfig.json; LatestMajor picks the highest installed one.
            # https://learn.microsoft.com/dotnet/core/versions/selection
            completed = run([str(tool), *validation.tool_arguments(state, package, references, baseline)],
                            cwd=root, check=False, env={**os.environ, "DOTNET_ROLL_FORWARD": "LatestMajor"})
            if completed.returncode or packages.sha256(package) != digest:
                raise QualificationError(f"Package validation of {package.name} failed.")

            result.append({"package": name, "project": project.relative_to(root).as_posix(),
                           "configuration": configuration, "nupkg": package.name, "sha256": digest,
                           "baselineVersion": state["properties"].get("PackageValidationBaselineVersion") or None})
    except validation.PackageValidationError as error:
        raise QualificationError(str(error)) from error

    return result


def read_trx(path: Path) -> list[dict]:
    """Return the results of a TRX file with test ID, outcome, assembly and skip reason."""
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as error:
        raise QualificationError(f"{path.name} is not a readable TRX file.") from error

    assemblies, layouts = {}, {}
    for test in root.iter(f"{TRX_NAMESPACE}UnitTest"):
        method = test.find(f"{TRX_NAMESPACE}TestMethod")
        code_base = method.get("codeBase") if method is not None else None
        if not test.get("id") or not code_base:
            raise QualificationError(f"{path.name} has a test definition without its assembly.")

        adapter = method.get("adapterTypeName", "")
        layout = next((path for pattern, path in TRX_SKIP_REASONS if pattern.match(adapter)), None)
        if layout is None:
            raise QualificationError(f"{path.name} comes from the test adapter {adapter or '(none)'}, whose TRX "
                                     "layout is not known; only the xUnit v3 VSTest adapter and the xUnit report "
                                     "of the Microsoft Testing Platform are read.")

        assemblies[test.get("id")] = Path(code_base.replace("\\", "/")).stem
        layouts[test.get("id")] = layout

    # WHY: The summary must agree with the results: NestedSet requires Counters with total equal to the number of
    # results and failed 0, MySql failed 0. A run that errored, timed out or was aborted can still contain only
    # passing results, so every failure counter of the TRX summary must be 0 too.
    summary = root.find(f"{TRX_NAMESPACE}ResultSummary")
    counters = summary.find(f"{TRX_NAMESPACE}Counters") if summary is not None else None
    if counters is None:
        raise QualificationError(f"{path.name} has no result summary.")

    if summary.get("outcome") in TRX_FAILED_OUTCOMES or any(counters.get(name, "0") != "0" for name in TRX_FAILURES):
        raise QualificationError(f"{path.name} reports a failed, errored, timed out or aborted run.")

    results = []
    for result in root.iter(f"{TRX_NAMESPACE}UnitTestResult"):
        identity = result.get("testId")
        if identity not in assemblies:
            raise QualificationError(f"{path.name} has a result without a test definition.")

        message = result.find("/".join(f"{TRX_NAMESPACE}{name}" for name in layouts[identity]))
        results.append({"testId": identity, "outcome": result.get("outcome"), "assembly": assemblies[identity],
                        "reason": (message.text or "").strip() if message is not None else ""})

    if counters.get("total") != str(len(results)):
        raise QualificationError(f"{path.name} summarizes another number of results than it contains.")

    return results


def check_tests(trx_files: list[Path], projects: dict[str, str], skip_projects: set[str]) -> list[dict]:
    """Require complete, passing and duplicate-free results for every test project."""
    if not trx_files:
        raise QualificationError("No TRX test results were provided.")

    summary = {name: {"project": path, "passed": 0, "skipped": 0, "trx": []} for name, path in projects.items()}
    seen = {}
    for path in sorted(trx_files):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        touched = set()
        for result in read_trx(path):
            assembly = result["assembly"]
            if assembly not in summary:
                raise QualificationError(f"{path.name} contains results of {assembly}, which is not a test project.")

            # WHY: Test runs may be split by filters only into disjoint sets; the same test twice means a split
            # overlaps or a result was counted twice.
            key = (assembly, result["testId"])
            if key in seen:
                raise QualificationError(f"Test {result['testId']} of {assembly} appears in {seen[key]} and "
                                         f"{path.name}.")

            seen[key] = path.name
            touched.add(assembly)
            outcome = result["outcome"]
            if outcome == "Passed":
                summary[assembly]["passed"] += 1
            elif outcome == "NotExecuted":
                # WHY: Skips are accepted only in projects the policy names, and only with a reason; whether the
                # reason is right is checked by the repository's own tests (for MySql its specification ledger).
                if summary[assembly]["project"] not in skip_projects and assembly not in skip_projects:
                    raise QualificationError(f"{assembly} skipped test {result['testId']}; the policy accepts no "
                                             "skips there.")

                if not result["reason"]:
                    raise QualificationError(f"{assembly} skipped test {result['testId']} without a reason.")

                summary[assembly]["skipped"] += 1
            else:
                raise QualificationError(f"Test {result['testId']} of {assembly} ended with {outcome}.")

        for assembly in touched:
            summary[assembly]["trx"].append({"file": path.name, "sha256": digest})

    empty = sorted(name for name, item in summary.items() if item["passed"] + item["skipped"] == 0)
    if empty:
        raise QualificationError(f"Test projects without results: {', '.join(empty)}.")

    return [{"assembly": name, **item} for name, item in sorted(summary.items())]


def documentation_chain(root: Path, repository: str, tree: str, source_ref: str, workflow: str, *,
                        verify=attestation.verify) -> tuple[dict, list[str]]:
    """Return the full pull request qualification behind the tree and the documentation-only trees on the way."""
    chain, current = [], tree
    with tempfile.TemporaryDirectory(prefix="pr-chain-") as directory:
        for _ in range(MAX_DOCUMENTATION_CHAIN):
            subject = attestation.tree_subject(Path(directory), current)
            pr = verify_pr_qualification(subject, repository, current, source_ref, workflow, verify=verify)
            scope = pr["predicate"].get("scope")
            if scope == "full":
                return pr, chain

            base = pr["predicate"].get("baseTree")
            if scope != "docs" or not isinstance(base, str):
                raise QualificationError(f"The pull request qualification of {current} has no valid scope.")

            # WHY: The documentation-only claim is recomputed from the trees with this checkout's policy and MSBuild
            # files; .github/** and MSBuild files are never documentation, so a step cannot change either.
            try:
                result = change_scope.classify(root, base, current)
            except change_scope.ScopeError as error:
                raise QualificationError(f"Cannot compare {base} with {current}: {error}") from error

            if result["scope"] != "docs":
                raise QualificationError(f"The change from {base} to {current} is not documentation only: "
                                         f"{', '.join(result['nonDocumentation'])}.")

            chain.append(current)
            current = base

    raise QualificationError(f"More than {MAX_DOCUMENTATION_CHAIN} documentation-only pull requests follow the last "
                             "fully qualified tree.")


def verify_pr_qualification(subject: Path, repository: str, tree: str, source_ref: str, workflow: str, *,
                            verify=attestation.verify) -> dict:
    """Return the newest qualification of the tree by the workflow for a pull request into the line."""
    # WHY: Each PR workflow attests only its own gates (pr_predicate), so a gate counts only in an attestation of
    # the workflow the policy names for it.
    matching = [item for item in verify(subject, repository, attestation.PR_QUALIFICATION)
                if item["predicate"].get("tree") == tree and item["predicate"].get("repository") == repository
                and item["predicate"].get("baseRef") == source_ref and item["predicate"].get("workflow") == workflow]
    if not matching:
        raise QualificationError(f"No pull request into {source_ref} qualified tree {tree} with {workflow}.")

    return attestation.latest(matching)


def protected_gates(root: Path, repository: str, tree: str, source_ref: str, policy: dict, *,
                    verify=attestation.verify) -> list[dict]:
    """Return every protected-check gate of the policy with the pull request qualification that attested it."""
    gates = []
    protected = {}
    for gate in policy["qualification"]["gates"]:
        if gate["kind"] == "protected-check":
            protected.setdefault(gate["workflow"], []).append(gate["name"])

    for workflow, names in sorted(protected.items()):
        pr, chain = documentation_chain(root, repository, tree, source_ref, workflow, verify=verify)
        attested = {gate["name"] for gate in pr["predicate"].get("gates", []) if gate.get("conclusion") == "success"}
        missing = sorted(set(names) - attested)
        if missing:
            raise QualificationError(f"The pull request qualification of {workflow} lacks gates: {', '.join(missing)}.")

        gates.extend({"name": name, "kind": "protected-check", "workflow": workflow, "runId": pr["runId"],
                      "attempt": pr["attempt"], "pullRequest": pr["predicate"].get("pullRequest"),
                      "tree": pr["predicate"].get("tree"), "documentationTrees": chain} for name in names)

    return gates


def qualify(api, environment: dict, root: Path, *, version: str, qualification_only: bool, trx_dir: Path,
            output: Path, package_dir: Path, pack_configuration: str = "Release", verify=attestation.verify,
            run=subprocess.run, fetcher=None, verifier=None) -> dict:
    """Qualify the checked-out candidate and write the subject and both predicates; return a summary."""
    repository = environment.get("GITHUB_REPOSITORY", "")
    source_ref = environment.get("GITHUB_REF", "")
    commit = environment.get("GITHUB_SHA", "")
    if git_tree(root, "HEAD") != git_tree(root, commit):
        raise QualificationError("The checkout is not the commit of this run.")

    from tools.release_lines.coordinator import observe_authority

    authority = observe_authority(api, repository)
    policy = authority["policy"]
    snapshot = policies.freeze_line(policy, source_ref, version, policy_commit=authority["commit"],
                                    qualification_only=qualification_only)
    tree = git_tree(root, commit)
    subject = attestation.tree_subject(output, tree)
    gates = protected_gates(root, repository, tree, source_ref, policy, verify=verify)
    produced = [gate["name"] for gate in policy["qualification"]["gates"] if gate["kind"] == "candidate-produced"]
    foreign = sorted(gate["name"] for gate in policy["qualification"]["gates"]
                     if gate["kind"] == "candidate-produced" and gate["workflow"] != workflow_path(environment))
    if foreign:
        raise QualificationError(f"The gates {', '.join(foreign)} belong to another workflow than this release run.")

    if produced:
        for gate in require_gate_jobs(run_jobs(api, repository, environment["GITHUB_RUN_ID"]), produced):
            gates.append({**gate, "kind": "candidate-produced", "runId": int(environment["GITHUB_RUN_ID"]),
                          "attempt": int(environment["GITHUB_RUN_ATTEMPT"])})

    skips = {item["project"] for item in policy["qualification"]["testResults"]["skips"]}
    tests = check_tests(sorted(trx_dir.rglob("*.trx")), test_projects(root), skips)
    with tempfile.TemporaryDirectory(prefix="package-validation-") as work:
        validated = require_package_validation(root, policy["release"]["packages"], package_dir=package_dir,
                                               version=version, configuration=pack_configuration, work=Path(work),
                                               run=run, fetcher=fetcher, verifier=verifier)
    attestation.write_predicate(output / "snapshot.json", snapshot)
    attestation.write_predicate(output / "qualification.json", {
        "schemaVersion": 1, "repository": repository, "sourceRef": source_ref, "commit": commit, "tree": tree,
        "version": version, "snapshot": snapshot, "gates": gates, "tests": tests, "packageValidation": validated})
    return {"tree": tree, "subject": str(subject), "gates": len(gates), "testProjects": len(tests)}
