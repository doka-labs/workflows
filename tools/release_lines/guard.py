"""Decide whether a workload run may proceed, based on how it was started.

- A run dispatched by the coordinator must prove it belongs to an active line,
  to the exact commit the coordinator resolved and to that coordinator run.
- A scheduled run on main proceeds only while main is an active line and the
  run is on the current head of main.
- Every other run (pull request, push, manual dispatch) proceeds unrestricted.
"""

from __future__ import annotations

from tools.release_lines import policy as policies
from tools.release_lines.coordinator import CORRELATION, SHA, observe_authority
from tools.release_lines.github import GitHubError


def _workload_for(policy: dict, workflow_ref: str, source_ref: str) -> dict:
    """Return the workload whose workflow is the running workflow at the running ref."""
    repository = policy["repository"]
    for workload in policy["workloads"]:
        if workflow_ref == f"{repository}/{workload['workflow']}@{source_ref}":
            return workload

    raise GitHubError("The running workflow is not a workload of the release policy.", code="authority_mismatch")


def decide(api, environment: dict, *, maintenance: bool, correlation: str, source_sha: str) -> bool:
    """Return whether the run may proceed; raise when a maintenance or scheduled run cannot prove its origin."""
    event = environment.get("GITHUB_EVENT_NAME")
    if not maintenance and event != "schedule":
        return True

    repository = environment.get("GITHUB_REPOSITORY", "")
    authority = observe_authority(api, repository)
    policy = authority["policy"]
    source_ref = environment.get("GITHUB_REF", "")
    _workload_for(policy, environment.get("GITHUB_WORKFLOW_REF", ""), source_ref)
    if maintenance:
        if (event != "workflow_dispatch" or not SHA.fullmatch(source_sha)
                or environment.get("GITHUB_SHA") != source_sha or not CORRELATION.fullmatch(correlation)):
            raise GitHubError("Maintenance run does not match its dispatched identity.", code="authority_mismatch")

        active = {line["sourceRef"] for line in policies.lines_for(policy, "maintenance")}
        if source_ref == policies.AUTHORITY_REF or source_ref not in active:
            raise GitHubError("Maintenance runs only on active lines besides main.", code="authority_mismatch")

        return True

    if source_ref != policies.AUTHORITY_REF or environment.get("GITHUB_SHA") != authority["commit"]:
        raise GitHubError("A scheduled workload runs only on the current head of main.", code="authority_mismatch")

    # WHY: The coordinator covers the other lines; main's own schedule covers main only while main is active, so
    # an inactive main neither runs twice nor audits a graph no line publishes (MySql's rule for its audit).
    return any(line["sourceRef"] == policies.AUTHORITY_REF for line in policies.lines_for(policy, "maintenance"))
