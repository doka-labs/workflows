"""Release policy of a product repository: lines, toolchain, workloads and qualification gates.

The policy lives in `.github/release-policy.json` of the product repository.
Reading a policy grants nothing by itself: callers that publish or dispatch
must read it from the protected authority ref and check it there.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICY_PATH = ".github/release-policy.json"
# WHY: Scheduled workflows run only on the default branch, so main is the one place a coordinator can run.
# https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
AUTHORITY_REF = "refs/heads/main"
# WHY: A policy is a few KiB; the bound keeps a malformed or hostile file from being read without limit.
MAX_POLICY_BYTES = 1024 * 1024
POLICY_FIELDS = {"schemaVersion", "revision", "repository", "authorityRef", "release", "toolchain", "lines",
                 "workloads", "qualification"}
RELEASE_FIELDS = {"workflowPath", "environment"}
TOOLCHAIN_FIELDS = {"frameworkPackages", "majorCeilingPackages", "coherentGroups", "lockfiles", "expectedLockfiles",
                    "versionPrefix", "coupling"}
LINE_FIELDS = {"sourceRef", "role", "packageMajor", "channel", "upstreamStage", "state"}
WORKLOAD_FIELDS = {"id", "workflow", "requiredJobs", "timeoutMinutes"}
QUALIFICATION_FIELDS = {"gates", "testResults"}
GATE_FIELDS = {"name", "kind", "workflow"}
TEST_RESULT_FIELDS = {"skips"}
SKIP_FIELDS = {"project"}
SNAPSHOT_FIELDS = {"schemaVersion", "repository", "sourceRef", "role", "packageMajor", "upstreamStage", "channel",
                   "stateAtQualification", "policyRevision", "policyDigest", "policyCommit", "qualificationOnly"}
PURPOSES = {"pr": ("inactive", "prepared", "active"), "qualification": ("prepared", "active"),
            "publication": ("active",), "maintenance": ("active",)}
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
# WHY: The revision token of the existing policies of MySql and SafeMigrations.
REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9a-z.-]+))?\Z")
WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml\Z")
IDENTIFIER = re.compile(r"[a-z][a-z0-9-]*\Z")
PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?:\.\*)?\Z")
RELATIVE_PATTERN = re.compile(r"[A-Za-z0-9_.*/-]+\Z")
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PolicyError(RuntimeError):
    """The release policy, a version or a frozen snapshot cannot prove its identity."""


@dataclass(frozen=True)
class ReleaseVersion:
    """Canonical three-part NuGet/SemVer identity."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...]

    @property
    def is_prerelease(self) -> bool:
        """Return whether this identity precedes its corresponding stable version."""
        return bool(self.prerelease)


def parse_version(value: Any) -> ReleaseVersion:
    """Parse a canonical version and reject aliases and build metadata that NuGet would normalize."""
    # WHY: NuGet removes leading zeros and SemVer build metadata when it matches versions, so "1.01.0" or
    # "1.0.0+x" would name the same package as "1.1.0" or "1.0.0". Only the canonical spelling is accepted.
    # The 64-character bound and the hyphen rules are those of SafeMigrations (eng/release/lines.py).
    # https://learn.microsoft.com/nuget/concepts/package-versioning
    match = VERSION.fullmatch(value) if isinstance(value, str) and len(value) <= 64 else None
    if match is None:
        raise PolicyError(f"Version must be canonical lowercase three-part NuGet/SemVer: {value!r}.")

    identifiers = tuple(match[4].split(".")) if match[4] else ()
    if any(not part or part.startswith("-") or part.endswith("-")
           or (part.isdigit() and len(part) > 1 and part[0] == "0") for part in identifiers):
        raise PolicyError(f"Version has a noncanonical prerelease identifier: {value!r}.")

    return ReleaseVersion(int(match[1]), int(match[2]), int(match[3]), identifiers)


def compare_versions(left: str, right: str) -> int:
    """Compare two canonical versions by SemVer precedence and return -1, 0 or 1."""
    # WHY: Precedence rules of SemVer 2.0.0 item 11. https://semver.org/spec/v2.0.0.html
    first, second = parse_version(left), parse_version(right)
    first_core, second_core = (first.major, first.minor, first.patch), (second.major, second.minor, second.patch)
    if first_core != second_core:
        return (first_core > second_core) - (first_core < second_core)

    if not first.prerelease or not second.prerelease:
        return int(not first.prerelease) - int(not second.prerelease)

    for first_part, second_part in zip(first.prerelease, second.prerelease):
        if first_part == second_part:
            continue

        if first_part.isdigit() and second_part.isdigit():
            return (int(first_part) > int(second_part)) - (int(first_part) < int(second_part))

        if first_part.isdigit() != second_part.isdigit():
            return -1 if first_part.isdigit() else 1

        return (first_part > second_part) - (first_part < second_part)

    return (len(first.prerelease) > len(second.prerelease)) - (len(first.prerelease) < len(second.prerelease))


def _object(value: Any, fields: set[str], label: str) -> dict:
    """Require exactly the given fields so no unconsumed setting can hide in the file."""
    if not isinstance(value, dict) or set(value) != fields:
        raise PolicyError(f"{label} must contain exactly these fields: {', '.join(sorted(fields))}.")

    return value


def _choice(value: Any, choices: tuple[str, ...], label: str) -> str:
    """Validate a string enum without coercion."""
    if not isinstance(value, str) or value not in choices:
        raise PolicyError(f"{label} must be one of: {', '.join(choices)}.")

    return value


def _positive(value: Any, label: str, *, maximum: int | None = None) -> int:
    """Validate a positive JSON integer that is not a Boolean."""
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        bound = f" between 1 and {maximum}" if maximum is not None else ""
        raise PolicyError(f"{label} must be a positive integer{bound}.")

    return value


def _strings(value: Any, pattern: re.Pattern, label: str, *, allow_empty: bool = False) -> list[str]:
    """Validate a list of distinct strings that each match the pattern."""
    if (not isinstance(value, list) or (not value and not allow_empty)
            or any(not isinstance(item, str) or pattern.fullmatch(item) is None for item in value)
            or len(set(value)) != len(value)):
        raise PolicyError(f"{label} must be a list of distinct valid entries.")

    return value


def _line(line: Any) -> dict:
    """Validate one release line: exact ref per role, channel per role, GA upstream outside preview."""
    # WHY: These are the identical line rules of MySql, SafeMigrations and NestedSet (eng/release/lines.py).
    _object(line, LINE_FIELDS, "Release line")
    role = _choice(line["role"], ("main", "maintenance", "preview"), "Release-line role")
    major = _positive(line["packageMajor"], "Release-line packageMajor")
    expected_ref = AUTHORITY_REF if role == "main" else f"refs/heads/{role}/{major}.x"
    if line["sourceRef"] != expected_ref:
        raise PolicyError(f"Release-line sourceRef must be exactly {expected_ref}.")

    stage = _choice(line["upstreamStage"], ("ga", "preview", "rc"), "Release-line upstreamStage")
    if role != "preview" and stage != "ga":
        raise PolicyError("Main and maintenance lines require GA upstream dependencies.")

    channel = _choice(line["channel"], ("stable-and-prerelease", "prerelease"), "Release-line channel")
    if (role == "preview") != (channel == "prerelease"):
        raise PolicyError("Only preview lines use the prerelease-only channel.")

    _choice(line["state"], ("inactive", "prepared", "active"), "Release-line state")

    return line


def _lines(entries: Any) -> None:
    """Require one main line, unique refs and at most one active line per package major."""
    if not isinstance(entries, list) or not entries:
        raise PolicyError("Release policy requires a nonempty lines array.")

    refs, owners = set(), set()
    for line in map(_line, entries):
        if line["sourceRef"] in refs:
            raise PolicyError(f"Duplicate release-line sourceRef: {line['sourceRef']}.")

        refs.add(line["sourceRef"])
        # WHY: Two active lines for one major would let two branches publish into the same package versions.
        if line["state"] == "active":
            if line["packageMajor"] in owners:
                raise PolicyError(f"Only one active line may own package major {line['packageMajor']}.")

            owners.add(line["packageMajor"])

    if AUTHORITY_REF not in refs:
        raise PolicyError("Release policy must contain exactly one main line.")


def _toolchain(value: Any) -> None:
    """Validate the inputs of the toolchain check."""
    _object(value, TOOLCHAIN_FIELDS, "Toolchain")
    _strings(value["frameworkPackages"], PACKAGE_PATTERN, "Toolchain frameworkPackages")
    _strings(value["majorCeilingPackages"], PACKAGE_PATTERN, "Toolchain majorCeilingPackages", allow_empty=True)
    overlap = {item.lower() for item in value["frameworkPackages"]} & {
        item.lower() for item in value["majorCeilingPackages"]}
    if overlap:
        raise PolicyError(f"Toolchain package patterns appear in both lists: {', '.join(sorted(overlap))}.")
    groups = value["coherentGroups"]
    if not isinstance(groups, list):
        raise PolicyError("Toolchain coherentGroups must be a list.")

    for group in groups:
        _strings(group, PACKAGE_PATTERN, "Toolchain coherent group")

    _strings(value["lockfiles"], RELATIVE_PATTERN, "Toolchain lockfiles")
    if any(item.startswith("/") or ".." in item.split("/") for item in value["lockfiles"]):
        raise PolicyError("Toolchain lockfiles must be relative to the repository root.")

    _positive(value["expectedLockfiles"], "Toolchain expectedLockfiles")
    prefix = value["versionPrefix"]
    if prefix is not None and (not isinstance(prefix, str) or RELATIVE_PATTERN.fullmatch(prefix) is None
                               or "*" in prefix or prefix.startswith("/") or ".." in prefix.split("/")):
        raise PolicyError("Toolchain versionPrefix must be null or a relative file path.")

    _choice(value["coupling"], ("coupled", "independent"), "Toolchain coupling")


def _workloads(entries: Any) -> None:
    """Validate the workloads the coordinator dispatches on active lines."""
    if not isinstance(entries, list):
        raise PolicyError("Release policy workloads must be a list.")

    identifiers = set()
    for workload in entries:
        _object(workload, WORKLOAD_FIELDS, "Workload")
        if not isinstance(workload["id"], str) or IDENTIFIER.fullmatch(workload["id"]) is None:
            raise PolicyError("Workload id must be a lowercase identifier.")

        if workload["id"] in identifiers:
            raise PolicyError(f"Duplicate workload id: {workload['id']}.")

        identifiers.add(workload["id"])
        if not isinstance(workload["workflow"], str) or WORKFLOW.fullmatch(workload["workflow"]) is None:
            raise PolicyError("Workload workflow must be a path below .github/workflows/.")

        jobs = workload["requiredJobs"]
        if (not isinstance(jobs, list) or not jobs or len(set(jobs)) != len(jobs)
                or any(not isinstance(job, str) or not job.strip() or job != job.strip() for job in jobs)):
            raise PolicyError("Workload requiredJobs must be a nonempty list of distinct job names.")

        # WHY: The coordinator waits inside one hosted job, which GitHub ends after 6 hours; 30 minutes stay for
        # resolving, dispatching and reporting. https://docs.github.com/en/actions/reference/limits
        _positive(workload["timeoutMinutes"], "Workload timeoutMinutes", maximum=330)


def _qualification(value: Any) -> None:
    """Validate qualification gates and the projects whose skipped tests are accepted."""
    _object(value, QUALIFICATION_FIELDS, "Qualification")
    gates = value["gates"]
    if not isinstance(gates, list) or not gates:
        raise PolicyError("Qualification requires a nonempty gates list.")

    names = set()
    for gate in gates:
        _object(gate, GATE_FIELDS, "Qualification gate")
        if not isinstance(gate["name"], str) or not gate["name"].strip() or gate["name"] in names:
            raise PolicyError("Qualification gate names must be distinct and nonempty.")

        names.add(gate["name"])
        _choice(gate["kind"], ("protected-check", "candidate-produced"), "Qualification gate kind")
        if not isinstance(gate["workflow"], str) or WORKFLOW.fullmatch(gate["workflow"]) is None:
            raise PolicyError("Qualification gate workflow must be a path below .github/workflows/.")

    results = _object(value["testResults"], TEST_RESULT_FIELDS, "Qualification testResults")
    skips = results["skips"]
    if not isinstance(skips, list):
        raise PolicyError("Qualification testResults skips must be a list.")

    projects = set()
    for skip in skips:
        _object(skip, SKIP_FIELDS, "Accepted skip project")
        project = skip["project"]
        if not isinstance(project, str) or not project.strip() or project in projects:
            raise PolicyError("Accepted skip projects must be distinct and nonempty.")

        projects.add(project)


def validate_policy(value: Any, *, repository: str | None = None) -> dict:
    """Validate a complete policy; with `repository`, also require that it belongs to that repository."""
    policy = _object(value, POLICY_FIELDS, "Release policy")
    if type(policy["schemaVersion"]) is not int or policy["schemaVersion"] != 1:
        raise PolicyError("Release policy requires integer schemaVersion 1.")

    if not isinstance(policy["revision"], str) or REVISION.fullmatch(policy["revision"]) is None:
        raise PolicyError("Release policy revision must be an ASCII token of at most 128 characters.")

    if not isinstance(policy["repository"], str) or REPOSITORY.fullmatch(policy["repository"]) is None:
        raise PolicyError("Release policy repository must be owner/name.")

    if repository is not None and policy["repository"] != repository:
        raise PolicyError(f"Release policy belongs to {policy['repository']}, not to {repository}.")

    if policy["authorityRef"] != AUTHORITY_REF:
        raise PolicyError(f"Release policy authorityRef must be {AUTHORITY_REF}.")

    release = _object(policy["release"], RELEASE_FIELDS, "Release")
    if not isinstance(release["workflowPath"], str) or WORKFLOW.fullmatch(release["workflowPath"]) is None:
        raise PolicyError("Release workflowPath must be a path below .github/workflows/.")

    if not isinstance(release["environment"], str) or IDENTIFIER.fullmatch(release["environment"]) is None:
        raise PolicyError("Release environment must be a lowercase identifier.")

    _toolchain(policy["toolchain"])
    _lines(policy["lines"])
    _workloads(policy["workloads"])
    _qualification(policy["qualification"])

    return copy.deepcopy(policy)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    """Reject duplicate JSON names, which ordinary parsing would silently collapse."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError(f"Release policy contains a duplicate property: {key}.")

        result[key] = value

    return result


def parse_policy(data: bytes | str, *, repository: str | None = None) -> dict:
    """Parse and validate policy bytes, for example as read from the authority ref through the API."""
    if len(data) > MAX_POLICY_BYTES:
        raise PolicyError("Release policy exceeds 1 MiB.")

    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, TypeError) as error:
        raise PolicyError("Release policy is not valid JSON.") from error

    return validate_policy(value, repository=repository)


def load_policy(root: Path, *, repository: str | None = None) -> dict:
    """Read the policy file of a checkout; it must be a regular file, not a symbolic link."""
    path = root / POLICY_PATH
    if path.is_symlink() or not path.is_file():
        raise PolicyError(f"{POLICY_PATH} must be a regular file.")

    if path.stat().st_size > MAX_POLICY_BYTES:
        raise PolicyError("Release policy exceeds 1 MiB.")

    return parse_policy(path.read_bytes(), repository=repository)


def policy_digest(policy: dict) -> str:
    """Hash the complete validated policy independently of JSON layout and key order."""
    encoded = json.dumps(validate_policy(policy), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def version_channel(line: dict, version: str) -> str:
    """Return the package channel of a version and require that the line may publish it."""
    parsed = parse_version(version)
    if parsed.major != line["packageMajor"]:
        raise PolicyError(f"Version {version} does not match package major {line['packageMajor']} of its line.")

    if line["role"] == "preview" and not parsed.is_prerelease:
        raise PolicyError("A preview line cannot publish a stable package.")

    return "prerelease" if parsed.is_prerelease else "stable"


def select_line(policy: dict, source_ref: str, version: str | None = None, *, purpose: str) -> dict:
    """Select the exact registered line of a ref for one purpose; selection grants nothing by itself."""
    if purpose not in PURPOSES:
        raise PolicyError(f"Line selection purpose must be one of: {', '.join(PURPOSES)}.")

    line = next((entry for entry in validate_policy(policy)["lines"] if entry["sourceRef"] == source_ref), None)
    if line is None or line["state"] not in PURPOSES[purpose]:
        raise PolicyError(f"{source_ref} is not a registered line eligible for {purpose}.")

    if version is not None:
        version_channel(line, version)

    return line


def lines_for(policy: dict, purpose: str) -> list[dict]:
    """Return every line eligible for a purpose, in policy order."""
    if purpose not in PURPOSES:
        raise PolicyError(f"Line selection purpose must be one of: {', '.join(PURPOSES)}.")

    return [line for line in validate_policy(policy)["lines"] if line["state"] in PURPOSES[purpose]]


def check_pr_base(policy: dict | None, base_ref: str) -> None:
    """Allow pull requests only against registered lines; without a policy on main, only against main."""
    # WHY: The first policy itself arrives through a pull request against main (as in NestedSet's check_pr).
    if policy is None:
        if base_ref != AUTHORITY_REF:
            raise PolicyError("Without a release policy on main, only main accepts pull requests.")

        return

    select_line(policy, base_ref, purpose="pr")


def freeze_line(policy: dict, source_ref: str, version: str, *, policy_commit: str,
                qualification_only: bool) -> dict:
    """Freeze the line identity observed during qualification into a snapshot."""
    if type(qualification_only) is not bool:
        raise PolicyError("qualificationOnly must be a Boolean.")

    purpose = "qualification" if qualification_only else "publication"
    line = select_line(policy, source_ref, version, purpose=purpose)
    snapshot = {key: line[key] for key in ("sourceRef", "role", "packageMajor", "upstreamStage")}
    snapshot.update(schemaVersion=1, repository=policy["repository"], channel=version_channel(line, version),
                    stateAtQualification=line["state"], policyRevision=policy["revision"],
                    policyDigest=policy_digest(policy), policyCommit=policy_commit,
                    qualificationOnly=qualification_only)

    return validate_snapshot(snapshot, source_ref=source_ref, version=version)


def validate_snapshot(snapshot: Any, *, source_ref: str | None = None, version: str | None = None,
                      repository: str | None = None) -> dict:
    """Validate a frozen snapshot independently of today's policy; deactivation does not revoke history."""
    value = _object(snapshot, SNAPSHOT_FIELDS, "Release-line snapshot")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise PolicyError("Release-line snapshot requires integer schemaVersion 1.")

    _line({**{key: value[key] for key in ("sourceRef", "role", "packageMajor", "upstreamStage")},
           "channel": "prerelease" if value["role"] == "preview" else "stable-and-prerelease",
           "state": value["stateAtQualification"]})
    _choice(value["stateAtQualification"], ("prepared", "active"), "Snapshot stateAtQualification")
    _choice(value["channel"], ("stable", "prerelease"), "Snapshot channel")
    if type(value["qualificationOnly"]) is not bool:
        raise PolicyError("Snapshot qualificationOnly must be a Boolean.")

    # WHY: A prepared line may be qualified but not published (NestedSet eng/release/lines.py validate_snapshot).
    if value["stateAtQualification"] == "prepared" and not value["qualificationOnly"]:
        raise PolicyError("A prepared line yields qualification-only snapshots.")

    if not isinstance(value["repository"], str) or REPOSITORY.fullmatch(value["repository"]) is None:
        raise PolicyError("Snapshot repository must be owner/name.")

    if not isinstance(value["policyRevision"], str) or REVISION.fullmatch(value["policyRevision"]) is None:
        raise PolicyError("Snapshot policyRevision is invalid.")

    if not isinstance(value["policyDigest"], str) or SHA256.fullmatch(value["policyDigest"]) is None:
        raise PolicyError("Snapshot policyDigest must be a lowercase SHA-256 digest.")

    if not isinstance(value["policyCommit"], str) or SHA1.fullmatch(value["policyCommit"]) is None:
        raise PolicyError("Snapshot policyCommit must be a full lowercase commit SHA.")

    expected = {"sourceRef": source_ref, "repository": repository}
    if any(item is not None and value[key] != item for key, item in expected.items()):
        raise PolicyError("Release-line snapshot does not match the expected release identity.")

    if version is not None and version_channel(value, version) != value["channel"]:
        raise PolicyError("Snapshot channel does not match the candidate version.")

    return copy.deepcopy(value)
