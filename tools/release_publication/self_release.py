"""Release of this repository itself: the immutable GitHub release of a maintainer's signed version tag.

doka-labs/workflows publishes no packages. A version is the signed tag
v<version> on a commit of main whose Self Check passed, with the notes of its
CHANGELOG.md section; GitHub attests the published immutable release.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from tools.release_lines import policy as policies
from tools.release_lines.policy import PolicyError
from tools.release_publication import publication
from tools.release_publication.publication import require

BRANCH = "main"
SELF_CHECK = ".github/workflows/self-check.yml"


def version_of(tag: str) -> str:
    """Return the canonical version a tag v<version> names."""
    require(tag.startswith("v"), f"{tag} is not a version tag v<version>.")
    try:
        policies.parse_version(tag[1:])
    except PolicyError as error:
        raise publication.PublicationError(f"{tag} does not name a canonical version.") from error

    return tag[1:]


def head(root: Path) -> str:
    """Return the commit of the checkout."""
    return publication.run(["git", "-C", str(root), "rev-parse", "HEAD"]).strip()


def on_branch(root: Path, commit: str) -> bool:
    """Return whether the commit belongs to the history of main on GitHub."""
    publication.run(["git", "-C", str(root), "fetch", "--no-tags", "origin",
                     f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}"])
    ancestor = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", commit, f"origin/{BRANCH}"],
                              capture_output=True, timeout=60, check=False)
    return ancestor.returncode == 0


def self_check(api, repository: str, commit: str) -> str:
    """Return success, pending or failure for the Self Check of the commit's push to main."""
    response = api.request(f"repos/{repository}/actions/workflows/self-check.yml/runs"
                           f"?head_sha={commit}&event=push&per_page=100")
    runs = response.get("workflow_runs") if isinstance(response, dict) else None
    require(isinstance(runs, list), "The Self Check run listing is invalid.")
    runs = [run for run in runs if run.get("head_sha") == commit and run.get("head_branch") == BRANCH
            and run.get("path") == SELF_CHECK]
    if any(run.get("status") == "completed" and run.get("conclusion") == "success" for run in runs):
        return "success"

    # WHY: The tag can follow the merge within seconds, before the push run of Self Check exists or finishes.
    if not runs or any(run.get("status") != "completed" for run in runs):
        return "pending"

    return "failure"


def wait_for_self_check(api, repository: str, commit: str, *, attempts: int, sleep) -> None:
    """Wait until the Self Check of the commit passed; fail when it failed or did not finish in time."""
    for attempt in range(attempts):
        state = self_check(api, repository, commit)
        require(state != "failure", f"Self Check failed on {commit}.")
        if state == "success":
            return

        require(attempt + 1 < attempts, f"Self Check did not pass on {commit} in time.")
        sleep(20)


def release(api, root: Path, repository: str, tag: str, *, attempts: int = 30, sleep=time.sleep,
            checkout=head, belongs=on_branch) -> dict:
    """Publish the immutable release of a signed version tag, or confirm the one already published."""
    version = version_of(tag)
    commit = publication.verified_tag_commit(api, repository, tag)
    require(checkout(root) == commit, f"The checkout is not the commit of {tag}.")
    require(belongs(root, commit), f"{commit} is not part of {BRANCH}.")
    wait_for_self_check(api, repository, commit, attempts=60, sleep=sleep)
    notes = publication.release_notes(root, version)
    prerelease = policies.parse_version(version).is_prerelease
    existing = publication.release_for(api, repository, tag)
    promote = False
    if existing is None:
        promote = publication.latest_flag(api, repository, version)
        # WHY: make_latest "Defaults to true for newly published releases", so it is always set explicitly.
        # Without assets there is nothing to attach to a draft first. https://docs.github.com/en/rest/releases/releases
        api.request(f"repos/{repository}/releases", "POST", {
            "tag_name": tag, "name": tag, "body": notes, "draft": False, "prerelease": prerelease,
            "make_latest": "true" if promote else "false"})
    else:
        # WHY: A rerun finds its own release and only verifies it; any other release of the tag is not this version.
        require(existing.get("draft") is False and existing.get("name") == tag and existing.get("body") == notes
                and existing.get("prerelease") is prerelease,
                f"A release of {tag} exists with another state, title, notes or prerelease flag.")

    # WHY: Publishing an immutable release "automatically generates a release attestation"; it can appear after the
    # release itself, so verification is retried within a fixed budget. The job token cannot read whether immutable
    # releases are enabled, so a release that never becomes immutable fails here.
    # https://docs.github.com/en/code-security/supply-chain-security/understanding-your-software-supply-chain/immutable-releases
    for attempt in range(attempts):
        published = publication.release_for(api, repository, tag)
        if published and published.get("draft") is False and published.get("immutable") is True:
            try:
                publication.run(["gh", "release", "verify", tag, "--repo", repository])
                break
            except publication.PublicationError:
                pass

        require(attempt + 1 < attempts, f"{tag} did not become an immutable, verified release in time.")
        sleep(10)

    if promote:
        latest = api.request(f"repos/{repository}/releases/latest")
        require(latest.get("tag_name") == tag, f"GitHub did not make {tag} Latest.")

    return {"tag": tag, "version": version, "commit": commit, "url": published.get("html_url", "")}
