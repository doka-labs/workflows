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
# WHY: The default predicate type of gh attestation verify ("default https://slsa.dev/provenance/v1") and the type
# of the provenance actions/attest writes, as in the release attestations of cli/cli 2.103.3.
# https://cli.github.com/manual/gh_attestation_verify
SLSA_PROVENANCE = "https://slsa.dev/provenance/v1"
SIGNERS = {
    PR_QUALIFICATION: f"{WORKFLOWS_REPOSITORY}/.github/workflows/release-pr-attestation.yml",
    RELEASE_LINE_SNAPSHOT: f"{WORKFLOWS_REPOSITORY}/.github/workflows/release-qualification.yml",
    RELEASE_QUALIFICATION: f"{WORKFLOWS_REPOSITORY}/.github/workflows/release-qualification.yml",
    RELEASE_ADMISSION: f"{WORKFLOWS_REPOSITORY}/.github/workflows/release-publish.yml",
    SLSA_PROVENANCE: f"{WORKFLOWS_REPOSITORY}/.github/workflows/release-candidate.yml",
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


def verify(subject: Path, repository: str, predicate_type: str, *, signer: str | None = None,
           source_digest: str | None = None, source_ref: str | None = None, run=subprocess.run) -> list[dict]:
    """Verify all attestations of a subject for a predicate type and return them with run identity and subjects."""
    command = ["gh", "attestation", "verify", str(subject), "--repo", repository,
               "--predicate-type", predicate_type, "--signer-workflow", signer or SIGNERS[predicate_type],
               "--deny-self-hosted-runners", "--format", "json"]
    # WHY: --source-digest and --source-ref "Enforce that the digest / git ref associated with the source repository
    # matches the provided value". https://cli.github.com/manual/gh_attestation_verify
    if source_digest:
        command.extend(["--source-digest", source_digest])

    if source_ref:
        command.extend(["--source-ref", source_ref])

    completed = run(command, capture_output=True, text=True, timeout=120, check=False)
    label = predicate_type.rsplit("/", 1)[-1]
    if completed.returncode:
        raise GitHubError(f"No valid {label} attestation for {subject.name}.", code="failed")

    try:
        entries = json.loads(completed.stdout)
    except ValueError as error:
        raise GitHubError("gh attestation verify returned invalid JSON.", code="github_invalid_response") from error

    result = []
    for entry in entries if isinstance(entries, list) else []:
        statement = entry.get("verificationResult", {}).get("statement", {})
        certificate = entry.get("verificationResult", {}).get("signature", {}).get("certificate", {})
        run_uri = RUN_URI.fullmatch(certificate.get("runInvocationURI", ""))
        subjects = statement.get("subject")
        if (statement.get("predicateType") != predicate_type or not isinstance(statement.get("predicate"), dict)
                or run_uri is None or run_uri[1] != repository or not isinstance(subjects, list)):
            raise GitHubError("A verified attestation lacks its predicate, subjects or run identity.", code="failed")

        result.append({"predicate": statement["predicate"], "runId": int(run_uri[2]), "attempt": int(run_uri[3]),
                       "sourceDigest": certificate.get("sourceRepositoryDigest"),
                       "sourceRef": certificate.get("sourceRepositoryRef"),
                       "subjects": {item.get("name"): item.get("digest", {}).get("sha256") for item in subjects
                                    if isinstance(item, dict)}})

    if not result:
        raise GitHubError(f"No valid {label} attestation for {subject.name}.", code="failed")

    return result


def exists(api, repository: str, subject: Path, predicate_type: str) -> bool:
    """Return whether an attestation of the predicate type names the subject; a failed read never means none."""
    import hashlib

    return exists_for_digest(api, repository, hashlib.sha256(subject.read_bytes()).hexdigest(), predicate_type)


def exists_for_digest(api, repository: str, digest: str, predicate_type: str) -> bool:
    """Return whether an attestation of the predicate type names the SHA-256 digest."""
    # WHY: GitHub answers 404 for a repository without attestations and 200 with an empty list for one without
    # attestations of this digest (observed for doka-labs and cli/cli); any other answer proves nothing, and a
    # failed read must never count as absence (SafeMigrations latest.py, release run 42767338686 attempt 2).
    # predicate_type is the "Optional filter for fetching attestations with a given predicate type"; without it the
    # provenance of candidate.json counted as an earlier admission. With the filter, a digest that carries only other
    # types also answers 404 (NestedSet v10.4.0 candidate.json: SLSA provenance listed, other types 404).
    # https://docs.github.com/en/rest/repos/attestations#list-attestations
    from urllib.parse import quote

    try:
        response = api.request(f"repos/{repository}/attestations/sha256:{digest}?per_page=1"
                               f"&predicate_type={quote(predicate_type, safe='')}")
    except GitHubError as error:
        if error.code == "github_http_error" and error.http_status == 404:
            return False

        raise

    entries = response.get("attestations") if isinstance(response, dict) else None
    if not isinstance(entries, list):
        raise GitHubError("The attestation listing is invalid.", code="github_invalid_response")

    return bool(entries)


def latest(attestations: list[dict]) -> dict:
    """Select the attestation of the greatest run, then the greatest attempt."""
    # WHY: The selection rule of MySql's evidence policy: greatest workflow run ID, then greatest run attempt.
    return max(attestations, key=lambda item: (item["runId"], item["attempt"]))


def write_predicate(path: Path, predicate: dict) -> Path:
    """Write a predicate file in canonical JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(predicate, indent=2, sort_keys=True) + "\n", encoding="ascii")
    return path
