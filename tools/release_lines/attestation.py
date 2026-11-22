"""Subjects, predicates and verification of the attestations the release workflows produce.

Attestations are signed by the reusable workflow of this repository that
creates them. `gh attestation verify` therefore checks the signer with
`--signer-workflow`: when a reusable workflow signs, "that reusable workflow is
the signer whose identity needs to be validated" (gh attestation verify --help).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from tools.release_lines.github import GitHubError

WORKFLOWS_REPOSITORY = "doka-labs/workflows"
PREDICATE_BASE = f"https://github.com/{WORKFLOWS_REPOSITORY}/blob/main/docs/predicates"
PR_QUALIFICATION = f"{PREDICATE_BASE}/pr-qualification-v1.md"
RELEASE_LINE_SNAPSHOT = f"{PREDICATE_BASE}/release-line-snapshot-v1.md"
RELEASE_QUALIFICATION = f"{PREDICATE_BASE}/release-qualification-v1.md"
RELEASE_ADMISSION = f"{PREDICATE_BASE}/release-admission-v1.md"
SIGNERS = {
    PR_QUALIFICATION: f"{WORKFLOWS_REPOSITORY}/.github/workflows/release-pr-attestation.yml",
    RELEASE_LINE_SNAPSHOT: f"{WORKFLOWS_REPOSITORY}/.github/workflows/release-qualification.yml",
    RELEASE_QUALIFICATION: f"{WORKFLOWS_REPOSITORY}/.github/workflows/release-qualification.yml",
    RELEASE_ADMISSION: f"{WORKFLOWS_REPOSITORY}/.github/workflows/release-publish.yml",
}
TREE = re.compile(r"[0-9a-f]{40}\Z")
RUN_URI = re.compile(r"https://github\.com/([^/]+/[^/]+)/actions/runs/([1-9][0-9]*)/attempts/([1-9][0-9]*)\Z")


def tree_subject(directory: Path, tree: str) -> Path:
    """Write the subject file that stands for a Git tree and return its path."""
    # WHY: An attestation subject needs a SHA-256 digest of a file; a Git tree ID is a SHA-1 name. The file
    # holds only the tree ID, so anyone can recreate the same subject from a checkout and verify it.
    if not TREE.fullmatch(tree):
        raise GitHubError("A Git tree ID must be 40 lowercase hexadecimal characters.", code="failed")

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"git-tree-{tree}.txt"
    path.write_text(f"{tree}\n", encoding="ascii")
    return path


def verify(subject: Path, repository: str, predicate_type: str, *, run=subprocess.run) -> list[dict]:
    """Verify all attestations of a subject for a predicate type and return them with their run identity."""
    command = ["gh", "attestation", "verify", str(subject), "--repo", repository,
               "--predicate-type", predicate_type, "--signer-workflow", SIGNERS[predicate_type],
               "--deny-self-hosted-runners", "--format", "json"]
    completed = run(command, capture_output=True, text=True, timeout=120, check=False)
    if completed.returncode:
        raise GitHubError(f"No valid {predicate_type.rsplit('/', 1)[-1]} attestation for {subject.name}.",
                          code="failed")

    try:
        entries = json.loads(completed.stdout)
    except ValueError as error:
        raise GitHubError("gh attestation verify returned invalid JSON.", code="github_invalid_response") from error

    result = []
    for entry in entries if isinstance(entries, list) else []:
        statement = entry.get("verificationResult", {}).get("statement", {})
        certificate = entry.get("verificationResult", {}).get("signature", {}).get("certificate", {})
        run_uri = RUN_URI.fullmatch(certificate.get("runInvocationURI", ""))
        if (statement.get("predicateType") != predicate_type or not isinstance(statement.get("predicate"), dict)
                or run_uri is None or run_uri[1] != repository):
            raise GitHubError("A verified attestation lacks its predicate or run identity.", code="failed")

        result.append({"predicate": statement["predicate"], "runId": int(run_uri[2]), "attempt": int(run_uri[3]),
                       "sourceDigest": certificate.get("sourceRepositoryDigest"),
                       "sourceRef": certificate.get("sourceRepositoryRef")})

    if not result:
        raise GitHubError(f"No valid {predicate_type.rsplit('/', 1)[-1]} attestation for {subject.name}.",
                          code="failed")

    return result


def latest(attestations: list[dict]) -> dict:
    """Select the attestation of the greatest run, then the greatest attempt."""
    # WHY: The selection rule of MySql's evidence policy: greatest workflow run ID, then greatest run attempt.
    return max(attestations, key=lambda item: (item["runId"], item["attempt"]))


def write_predicate(path: Path, predicate: dict) -> Path:
    """Write a predicate file in canonical JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(predicate, indent=2, sort_keys=True) + "\n", encoding="ascii")
    return path
