"""Qualification of a pull request and of a release candidate.

A pull request attests its tested merge tree once its gate jobs succeeded. A
release candidate is qualified when its commit's tree carries such a pull
request attestation, the candidate's own gate jobs succeeded in the release
run, and its test results are complete.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from tools.dependency_audit.audit import source_projects
from tools.release_lines import attestation
from tools.release_lines import policy as policies
from tools.release_lines.github import GitHubError

TRX_NAMESPACE = "{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}"
WORKFLOW_REF = re.compile(r"([^/]+/[^/]+)/(\.github/workflows/[^@]+)@(.+)\Z")


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
    # the run tested. https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request
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
    return tree, {"schemaVersion": 1, "repository": policy["repository"], "pullRequest": pull.get("number"),
                  "baseRef": base, "baseSha": pull.get("base", {}).get("sha"),
                  "headSha": pull.get("head", {}).get("sha"), "mergeCommit": merge, "tree": tree,
                  "workflow": path, "gates": gates}


def test_projects(root: Path) -> dict[str, str]:
    """Return the test projects of a restored checkout as assembly name to relative project path."""
    # WHY: Whether a project is a test project is decided by MSBuild evaluation (IsTestProject=true, set for example
    # by Microsoft.NET.Test.Sdk.props), not by the project XML: MySql has helper and smoke projects without the property, and
    # NestedSet's shared Specification.Tests library evaluates to false.
    result = {}
    for project in source_projects(root):
        completed = subprocess.run(["dotnet", "msbuild", str(project), "-nologo", "-getProperty:IsTestProject",
                                    "-getProperty:AssemblyName"], capture_output=True, text=True, timeout=300,
                                   check=False)
        try:
            properties = json.loads(completed.stdout)["Properties"]
        except (ValueError, KeyError, TypeError) as error:
            raise QualificationError(f"MSBuild cannot evaluate {project.relative_to(root)}.") from error

        if completed.returncode:
            raise QualificationError(f"MSBuild cannot evaluate {project.relative_to(root)}.")

        if properties.get("IsTestProject", "").lower() == "true":
            name = properties.get("AssemblyName") or project.stem
            if name in result:
                raise QualificationError(f"Two test projects build the assembly {name}.")

            result[name] = project.relative_to(root).as_posix()

    return result


def read_trx(path: Path) -> list[dict]:
    """Return the results of a TRX file with test ID, outcome, assembly and skip reason."""
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as error:
        raise QualificationError(f"{path.name} is not a readable TRX file.") from error

    assemblies = {}
    for test in root.iter(f"{TRX_NAMESPACE}UnitTest"):
        method = test.find(f"{TRX_NAMESPACE}TestMethod")
        code_base = method.get("codeBase") if method is not None else None
        if not test.get("id") or not code_base:
            raise QualificationError(f"{path.name} has a test definition without its assembly.")

        assemblies[test.get("id")] = Path(code_base.replace("\\", "/")).stem

    results = []
    for result in root.iter(f"{TRX_NAMESPACE}UnitTestResult"):
        identity = result.get("testId")
        if identity not in assemblies:
            raise QualificationError(f"{path.name} has a result without a test definition.")

        # WHY: xUnit writes the skip reason of a NotExecuted result to Output/ErrorInfo/Message, as in MySql's TRX of
        # run 44491092524 (327 skipped tests, all with a reason there); the TRX format has no public specification.
        message = result.find(f"{TRX_NAMESPACE}Output/{TRX_NAMESPACE}ErrorInfo/{TRX_NAMESPACE}Message")
        results.append({"testId": identity, "outcome": result.get("outcome"), "assembly": assemblies[identity],
                        "reason": (message.text or "").strip() if message is not None else ""})

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


def verify_pr_qualification(subject: Path, repository: str, tree: str, source_ref: str, *,
                            verify=attestation.verify) -> dict:
    """Return the newest pull request qualification of the tree for a pull request into the line."""
    matching = [item for item in verify(subject, repository, attestation.PR_QUALIFICATION)
                if item["predicate"].get("tree") == tree and item["predicate"].get("repository") == repository
                and item["predicate"].get("baseRef") == source_ref]
    if not matching:
        raise QualificationError(f"No pull request into {source_ref} qualified tree {tree}.")

    return attestation.latest(matching)


def qualify(api, environment: dict, root: Path, *, version: str, qualification_only: bool, trx_dir: Path,
            output: Path, verify=attestation.verify) -> dict:
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
    gates = []
    protected = [gate["name"] for gate in policy["qualification"]["gates"] if gate["kind"] == "protected-check"]
    if protected:
        pr = verify_pr_qualification(subject, repository, tree, source_ref, verify=verify)
        attested = {gate["name"] for gate in pr["predicate"].get("gates", []) if gate.get("conclusion") == "success"}
        missing = sorted(set(protected) - attested)
        if missing:
            raise QualificationError(f"The pull request qualification lacks gates: {', '.join(missing)}.")

        gates.extend({"name": name, "kind": "protected-check", "runId": pr["runId"], "attempt": pr["attempt"],
                      "pullRequest": pr["predicate"].get("pullRequest")} for name in protected)

    produced = [gate["name"] for gate in policy["qualification"]["gates"] if gate["kind"] == "candidate-produced"]
    if produced:
        for gate in require_gate_jobs(run_jobs(api, repository, environment["GITHUB_RUN_ID"]), produced):
            gates.append({**gate, "kind": "candidate-produced", "runId": int(environment["GITHUB_RUN_ID"]),
                          "attempt": int(environment["GITHUB_RUN_ATTEMPT"])})

    skips = {item["project"] for item in policy["qualification"]["testResults"]["skips"]}
    tests = check_tests(sorted(trx_dir.rglob("*.trx")), test_projects(root), skips)
    attestation.write_predicate(output / "snapshot.json", snapshot)
    attestation.write_predicate(output / "qualification.json", {
        "schemaVersion": 1, "repository": repository, "sourceRef": source_ref, "commit": commit, "tree": tree,
        "version": version, "snapshot": snapshot, "gates": gates, "tests": tests})
    return {"tree": tree, "subject": str(subject), "gates": len(gates), "testProjects": len(tests)}
