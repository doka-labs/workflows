"""Dispatch a workload on every active release line besides main and verify each dispatched run.

GitHub runs schedules on the default branch only. The coordinator therefore
runs on main and starts each other active line's own workflow through
`workflow_dispatch`, so every line keeps its own SDK, tests and workflow.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from tools.release_lines import policy as policies
from tools.release_lines.github import GitHubError, failure_observation

SHA = re.compile(r"[0-9a-f]{40}\Z")
# WHY: The correlation format and bounds of SafeMigrations' coordinator.
CORRELATION = re.compile(r"schedule-[1-9][0-9]{0,17}-[1-9][0-9]{0,8}\Z")
# WHY: A freshly dispatched run can answer 404 or show its default title for a while. NestedSet's coordinator
# failed on such a 404 although the run later succeeded (NestedSet PR #32); 300 seconds is its visibility window.
VISIBILITY_SECONDS = 300
# WHY: A run can report "completed" before its conclusion; SafeMigrations failed on conclusion null (PR #62) and
# waits 120 seconds for it.
RESULT_SECONDS = 120
# WHY: The polling interval of the MySql and SafeMigrations coordinators.
POLL_SECONDS = 15
PRE_EXECUTION = ("queued", "pending", "requested", "waiting")
STATES = (*PRE_EXECUTION, "in_progress", "completed")
CONCLUSIONS = (None, "success", "failure", "cancelled", "skipped", "timed_out", "neutral", "action_required",
               "stale", "startup_failure")
FORBIDDEN_JOB_CONCLUSIONS = ("failure", "cancelled", "timed_out")


def positive_identity(value: Any) -> int:
    """Return a bounded positive GitHub identifier, rejecting Booleans and text."""
    if type(value) is not int or not 0 < value < 10 ** 18:
        raise GitHubError("GitHub returned an invalid identifier.", code="dispatch_invalid")

    return value


def correlation_for(run_id: Any, attempt: Any) -> str:
    """Build the correlation that ties dispatched runs to this coordinator attempt."""
    correlation = f"schedule-{run_id}-{attempt}"
    if not CORRELATION.fullmatch(correlation):
        raise GitHubError("Coordinator run identity is invalid.", code="authority_mismatch")

    return correlation


def read_source(api, repository: str, path: str, commit: str) -> bytes:
    """Read a file at an exact commit and require that its content matches the Git blob identity."""
    response = api.request(f"repos/{repository}/contents/{quote(path)}?ref={commit}")
    if (not isinstance(response, dict) or response.get("type") != "file" or response.get("path") != path
            or response.get("encoding") != "base64" or not isinstance(response.get("content"), str)
            or len(response["content"]) > 2 * policies.MAX_POLICY_BYTES):
        raise GitHubError(f"{path} is unavailable at {commit}.", code="policy_invalid")

    try:
        raw = base64.b64decode("".join(response["content"].split()), validate=True)
    except ValueError as error:
        raise GitHubError(f"{path} has an invalid encoding.", code="policy_invalid") from error

    # WHY: Git names a blob by the SHA-1 of "blob <size>\0" plus the content; matching it proves the decoded
    # content is the file at that commit. https://git-scm.com/book/en/v2/Git-Internals-Git-Objects
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if response.get("sha") != blob:
        raise GitHubError(f"{path} differs from its Git blob.", code="policy_invalid")

    return raw


def protected_head(api, repository: str, source_ref: str) -> str:
    """Return the head commit of a branch and require that the branch exists and is protected."""
    branch = source_ref.removeprefix("refs/heads/")
    observed = api.request(f"repos/{repository}/branches/{quote(branch, safe='')}")
    commit = observed.get("commit", {}).get("sha", "") if isinstance(observed, dict) else ""
    if (not isinstance(observed, dict) or observed.get("name") != branch or not isinstance(commit, str)
            or not SHA.fullmatch(commit)):
        raise GitHubError(f"{source_ref} is missing or has no exact head commit.", code="branch_unprotected")

    # WHY: The field is true for branches protected by rulesets only; the doka-labs repositories use rulesets and
    # no classic branch protection, and their main and maintenance branches report true.
    # https://docs.github.com/en/rest/branches/branches#list-branches
    if observed.get("protected") is not True:
        raise GitHubError(f"{source_ref} is not protected.", code="branch_unprotected")

    return commit


def observe_authority(api, repository: str, *, allow_missing: bool = False) -> dict:
    """Read the release policy from the current head of protected main; policy is None only if allowed."""
    commit = protected_head(api, repository, policies.AUTHORITY_REF)
    try:
        raw = read_source(api, repository, policies.POLICY_PATH, commit)
    except GitHubError as error:
        if allow_missing and error.code == "github_http_error" and error.http_status == 404:
            return {"policy": None, "commit": commit}

        raise

    try:
        policy = policies.parse_policy(raw, repository=repository)
    except policies.PolicyError as error:
        raise GitHubError(f"Release policy on main is invalid: {error}", code="policy_invalid") from error

    return {"policy": policy, "commit": commit}


def require_coordinator_identity(authority: dict, environment: dict) -> None:
    """Allow coordination only from a run on the current head of main in the policy's repository."""
    repository = authority["policy"]["repository"]
    workflow_ref = environment.get("GITHUB_WORKFLOW_REF", "")
    if (environment.get("GITHUB_EVENT_NAME") not in ("schedule", "workflow_dispatch")
            or environment.get("GITHUB_REPOSITORY") != repository
            or environment.get("GITHUB_REF") != policies.AUTHORITY_REF
            or environment.get("GITHUB_SHA") != authority["commit"]
            or not workflow_ref.startswith(f"{repository}/.github/workflows/")
            or not workflow_ref.endswith(f"@{policies.AUTHORITY_REF}")):
        raise GitHubError("Only a run on the current head of protected main may coordinate release lines.",
                          code="authority_mismatch")


def verify_jobs(api, repository: str, run_id: int, required: list[str]) -> None:
    """Require every declared job to be present and successful and no job to fail, cancel or time out."""
    jobs, seen, total = [], set(), None
    # WHY: filter=latest lists the jobs of the latest attempt, so a successful re-run counts and an earlier failed
    # attempt does not; per_page is at most 100. The page and total bounds are SafeMigrations'.
    # https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run
    for page in range(1, 102):
        response = api.request(f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}")
        entries = response.get("jobs") if isinstance(response, dict) else None
        count = response.get("total_count") if isinstance(response, dict) else None
        if (type(count) is not int or not 0 < count <= 10000 or not isinstance(entries, list) or len(entries) > 100
                or (total is not None and total != count)):
            raise GitHubError("Job listing of the dispatched run is incomplete.", code="jobs_invalid")

        total = count
        for job in entries:
            identity = job.get("id") if isinstance(job, dict) else None
            if type(identity) is not int or identity in seen or not isinstance(job.get("name"), str):
                raise GitHubError("Job listing of the dispatched run repeats or lacks a job.", code="jobs_invalid")

            seen.add(identity)
            jobs.append(job)

        # WHY: A full page can coincide with a stale total; only a short page ends the listing.
        if len(entries) < 100:
            if len(jobs) != total:
                raise GitHubError("Job listing ended before the reported total.", code="jobs_invalid")

            break
    else:
        raise GitHubError("Job listing exceeded its page bound.", code="jobs_invalid")

    # WHY: A skipped job is accepted unless it is required, so optional jobs of a workload may be skipped by their
    # own conditions; failed, cancelled and timed-out jobs are never accepted.
    for job in jobs:
        if job.get("status") != "completed" or job.get("conclusion") in FORBIDDEN_JOB_CONCLUSIONS:
            raise GitHubError(f"Job '{job['name']}' did not complete cleanly.", code="jobs_unsuccessful")

        if job.get("conclusion") not in ("success", "skipped"):
            raise GitHubError(f"Job '{job['name']}' ended with an unaccepted conclusion.", code="jobs_unsuccessful")

    # WHY: GitHub names matrix jobs "<job> (<values>)"; every row of a required job must succeed (MySql's rule).
    for name in required:
        matches = [job for job in jobs if job["name"] == name or job["name"].startswith(f"{name} (")]
        if not matches or any(job.get("conclusion") != "success" for job in matches):
            raise GitHubError(f"Required job '{name}' is missing, skipped or unsuccessful.", code="jobs_unsuccessful")


def _check_identity(run: dict, identities: dict, expected_title: str, *, initialized: bool) -> list[str]:
    """Reject contradicting identities and return the fields GitHub has not filled in yet."""
    missing = []
    for field, target in identities.items():
        value = run.get(field)
        if value is None or value == "":
            missing.append(field)
            continue

        actual = value.split("@", 1)[0] if field == "path" and isinstance(value, str) else value
        if actual != target:
            raise GitHubError("Dispatched run contradicts its source identity.", code="child_identity_mismatch",
                              field=field)

    title = run.get("display_title")
    if title != expected_title:
        # WHY: The run-name can appear after the run itself (MySql commit 885efb328cba, SafeMigrations PR #62);
        # a different maintenance title is a real conflict.
        if initialized or (title is not None and not isinstance(title, str)) or (
                isinstance(title, str) and title.startswith("maintenance ")):
            raise GitHubError("Dispatched run has a different correlation title.", code="child_identity_mismatch",
                              field="display_title")

        missing.append("display_title")

    return missing


def wait_for_run(api, repository: str, run_id: int, workload: dict, source_ref: str, commit: str,
                 correlation: str, *, record: Callable[[dict], None], clock=time.monotonic,
                 sleep=time.sleep) -> dict:
    """Wait for a dispatched run within the workload budget and verify its identity and jobs."""
    started = clock()
    deadline = started + workload["timeoutMinutes"] * 60
    visibility_deadline = min(deadline, started + VISIBILITY_SECONDS)
    result_deadline = None
    initialized = False
    identities = {"event": "workflow_dispatch", "head_sha": commit,
                  "head_branch": source_ref.removeprefix("refs/heads/"), "path": workload["workflow"]}
    expected_title = f"maintenance {correlation}"

    def pause(until: float) -> None:
        sleep(min(POLL_SECONDS, max(0.0, until - clock())))

    while True:
        try:
            run = api.request(f"repos/{repository}/actions/runs/{run_id}")
        except GitHubError as error:
            # WHY: A freshly dispatched run can return 404 for a short time after dispatch.
            if initialized or error.code != "github_http_error" or error.http_status != 404:
                raise

            record({"observation": "run_not_visible"})
            if clock() >= visibility_deadline:
                raise GitHubError("Dispatched run did not become visible in time.", code="child_visibility_timeout",
                                  http_status=404) from error

            pause(visibility_deadline)
            continue

        if not isinstance(run, dict) or run.get("id") != run_id:
            raise GitHubError("Dispatched run has a different identity.", code="child_identity_mismatch", field="id")

        status, conclusion = run.get("status"), run.get("conclusion")
        if status not in STATES or conclusion not in CONCLUSIONS:
            raise GitHubError("Dispatched run has an unknown state.", code="child_state_invalid")

        if status != "completed" and conclusion is not None:
            raise GitHubError("Dispatched run reports a conclusion before completion.", code="child_state_invalid")

        record({"status": status, "conclusion": conclusion or "none"})
        missing = _check_identity(run, identities, expected_title, initialized=initialized)
        if status == "completed" and conclusion not in (None, "success"):
            raise GitHubError(f"Dispatched run ended with {conclusion}.", code="child_failed")

        now = clock()
        if missing:
            if initialized or (status not in PRE_EXECUTION and missing != ["display_title"]):
                raise GitHubError("Dispatched run lacks its identity.", code="child_identity_mismatch",
                                  field=missing[0])

            record({"observation": "identity_pending"})
            if now >= visibility_deadline:
                raise GitHubError("Identity of the dispatched run did not appear in time.",
                                  code="child_visibility_timeout")

            pause(visibility_deadline)
            continue

        if not initialized and now >= visibility_deadline:
            raise GitHubError("Identity of the dispatched run appeared too late.", code="child_visibility_timeout")

        if now >= deadline:
            raise GitHubError("Dispatched run exceeded the workload time budget.", code="child_wait_timeout")

        initialized = True
        if status == "completed" and conclusion is None:
            # WHY: GitHub can report completion before the conclusion; allow one bounded window, not a new budget.
            if result_deadline is None:
                result_deadline = min(deadline, now + RESULT_SECONDS)

            if now >= result_deadline:
                raise GitHubError("Completed run reported no conclusion in time.", code="child_result_timeout")

            record({"observation": "result_pending"})
            pause(result_deadline)
            continue

        if status == "completed":
            verify_jobs(api, repository, run_id, workload["requiredJobs"])

            return {"sourceRef": source_ref, "sourceCommit": commit, "runId": run_id, "conclusion": "success",
                    "verificationStatus": "success"}

        pause(deadline)


def dispatch_line(api, repository: str, workload: dict, source_ref: str, correlation: str, *,
                  record: Callable[[dict], None], wait=wait_for_run) -> dict:
    """Dispatch the workload on one line at its exact protected head and wait for the result."""
    commit = protected_head(api, repository, source_ref)
    record({"sourceCommit": commit, "verificationStatus": "resolved"})
    workflow = workload["workflow"].removeprefix(".github/workflows/")
    response = api.request(f"repos/{repository}/actions/workflows/{quote(workflow)}/dispatches", "POST", {
        "ref": source_ref.removeprefix("refs/heads/"),
        "inputs": {"maintenance": True, "schedule-correlation": correlation, "schedule-source-sha": commit}})
    # WHY: The dispatch response carries the run ID ("200 - Response including the workflow run ID"); searching
    # runs by time would race with other dispatches. https://docs.github.com/en/rest/actions/workflows
    run_id = positive_identity(response.get("workflow_run_id") if isinstance(response, dict) else None)
    record({"runId": run_id, "verificationStatus": "dispatched"})

    return wait(api, repository, run_id, workload, source_ref, commit, correlation, record=record)


def persist_report(path: Path, report: dict) -> None:
    """Replace the report atomically so an interrupted run still leaves a readable report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=path.parent, prefix=f".{path.name}.",
                                     delete=False) as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")

    Path(stream.name).replace(path)


def coordinate(api, environment: dict, workload_id: str, output: Path, *, dispatch=dispatch_line) -> dict:
    """Dispatch a workload on all active lines besides main and collect every outcome independently."""
    repository = environment.get("GITHUB_REPOSITORY", "")
    report = {"schemaVersion": 1, "workload": workload_id, "conclusion": "in_progress", "children": []}
    persist_report(output, report)
    try:
        authority = observe_authority(api, repository)
        require_coordinator_identity(authority, environment)
        workload = next((item for item in authority["policy"]["workloads"] if item["id"] == workload_id), None)
        if workload is None:
            raise GitHubError(f"Workload '{workload_id}' is not declared in the release policy.",
                              code="policy_invalid")

        correlation = correlation_for(environment.get("GITHUB_RUN_ID"), environment.get("GITHUB_RUN_ATTEMPT"))
    except GitHubError as error:
        report.update(conclusion="failure", failure=failure_observation(error))
        persist_report(output, report)
        raise

    targets = [line for line in policies.lines_for(authority["policy"], "maintenance")
               if line["sourceRef"] != policies.AUTHORITY_REF]
    report.update(authorityCommit=authority["commit"], correlation=correlation, workflow=workload["workflow"],
                  children=[{"sourceRef": line["sourceRef"], "verificationStatus": "pending"} for line in targets])
    lock = threading.Lock()

    def record(index: int, update: dict) -> None:
        with lock:
            report["children"][index].update(update)
            persist_report(output, report)

    persist_report(output, report)
    # WHY: SafeMigrations waits for up to four lines at a time; lines are independent dispatched runs.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [(index, executor.submit(dispatch, api, repository, workload, line["sourceRef"], correlation,
                                           record=lambda update, owner=index: record(owner, update)))
                   for index, line in enumerate(targets)]
        # WHY: Every future is collected so a failing line cannot hide the outcome of another.
        for index, future in futures:
            try:
                record(index, future.result())
            except (GitHubError, policies.PolicyError, OSError, subprocess.SubprocessError, ValueError) as error:
                record(index, failure_observation(error))

    report["conclusion"] = "success" if all(child["verificationStatus"] == "success"
                                            for child in report["children"]) else "failure"
    persist_report(output, report)

    return report
