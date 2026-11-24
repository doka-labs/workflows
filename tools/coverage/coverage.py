"""Coverage gate: merge Cobertura reports and enforce per-assembly and per-class thresholds.

The union of the gates of MySql (eng/tools/.../CoverageGate), SafeMigrations
(eng/verify-coverage.py) and NestedSet (eng/verify-coverage.py). The policy is
`.github/coverage-policy.json` in the product repository.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

POLICY_FIELDS = {"schemaVersion", "evidenceMaxAgeSeconds", "assemblies", "criticalClasses"}
ASSEMBLY_FIELDS = {"name", "minimumLinePercent", "minimumBranchPercent"}
CLASS_FIELDS = {"assembly", "name", "minimumLinePercent", "minimumBranchPercent"}
CONDITION = re.compile(r"\((\d+)/(\d+)\)")
# WHY: A report timestamp more than five minutes in the future is invalid (MySql's FreshnessErrors).
FUTURE_TOLERANCE_SECONDS = 300


class CoverageError(RuntimeError):
    """The policy or a report is invalid, or a threshold is not met."""


@dataclass
class Line:
    covered: bool = False
    covered_branches: int = 0
    total_branches: int = 0


def _threshold(entry: dict, fields: set[str], label: str) -> None:
    """Validate one threshold entry: percentages, and a branch floor that is null or above zero."""
    if not isinstance(entry, dict) or set(entry) != fields:
        raise CoverageError(f"{label} must contain exactly: {', '.join(sorted(fields))}.")

    line = entry["minimumLinePercent"]
    branch = entry["minimumBranchPercent"]
    if type(line) not in (int, float) or not 0 < line <= 100:
        raise CoverageError(f"{label} minimumLinePercent must be above 0 and at most 100.")

    # WHY: null declares a branch-free surface; a floor of 0 would accept untested branches (MySql's rule).
    if branch is not None and (type(branch) not in (int, float) or not 0 < branch <= 100):
        raise CoverageError(f"{label} minimumBranchPercent must be null or above 0 and at most 100.")


def load_policy(path: Path) -> dict:
    """Read and validate the coverage policy."""
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CoverageError(f"Cannot read {path.name}.") from error

    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS or policy["schemaVersion"] != 1:
        raise CoverageError(f"{path.name} must use schemaVersion 1 with exactly: {', '.join(sorted(POLICY_FIELDS))}.")

    age = policy["evidenceMaxAgeSeconds"]
    if age is not None and (type(age) is not int or age <= 0):
        raise CoverageError("evidenceMaxAgeSeconds must be null or a positive integer.")

    names = []
    for entry in policy["assemblies"]:
        _threshold(entry, ASSEMBLY_FIELDS, "An assembly threshold")
        names.append(entry["name"])

    if not names or len(names) != len(set(names)):
        raise CoverageError("The policy needs distinct assembly thresholds.")

    classes = []
    for entry in policy["criticalClasses"]:
        _threshold(entry, CLASS_FIELDS, "A critical class threshold")
        classes.append((entry["assembly"], entry["name"]))

    if len(classes) != len(set(classes)) or any(assembly not in names for assembly, _ in classes):
        raise CoverageError("Critical classes must be distinct and belong to a policy assembly.")

    return policy


def source_key(filename: str) -> str:
    """Normalize a source path so the same file from different runners merges (SafeMigrations)."""
    normalized = filename.replace("\\", "/")
    marker = normalized.find("/src/")
    return normalized[marker + 1:] if marker >= 0 else normalized


def read_reports(directory: Path, policy: dict, now: float) -> dict[str, dict[tuple[str, str, int], Line]]:
    """Merge every report line by assembly, source, class and line number."""
    reports = sorted(directory.rglob("*.cobertura.xml"))
    if not reports:
        raise CoverageError(f"No Cobertura reports below {directory}.")

    merged: dict[str, dict[tuple[str, str, int], Line]] = {}
    for report in reports:
        try:
            root = ElementTree.parse(report).getroot()
        except ElementTree.ParseError as error:
            raise CoverageError(f"{report.name} is not valid XML.") from error

        packages = root.findall("./packages/package")
        # WHY: A cancelled collection can leave instrumented copies that produce an empty report on the next run;
        # every report must hold lines so a healthy project cannot hide another's missing coverage (NestedSet).
        # WHY: The same direct class lines as the measurement below; coverlet also writes method lines, which do not
        # count.
        # https://github.com/coverlet-coverage/coverlet/blob/master/src/coverlet.core/Reporters/CoberturaReporter.cs
        if root.tag != "coverage" or not packages or not any(package.findall("./classes/class/lines/line")
                                                             for package in packages):
            raise CoverageError(f"{report} is an empty or invalid Cobertura report.")

        if policy["evidenceMaxAgeSeconds"] is not None:
            timestamp = int(root.get("timestamp", "0"))
            if timestamp <= 0 or timestamp - now > FUTURE_TOLERANCE_SECONDS:
                raise CoverageError(f"{report.name} has no valid timestamp.")

            if now - timestamp > policy["evidenceMaxAgeSeconds"]:
                raise CoverageError(f"{report.name} is older than {policy['evidenceMaxAgeSeconds']} seconds.")

        # WHY: coverlet writes file names relative to the common root of the instrumented files, so a report of one
        # assembly and a report of two name the same file differently; only the joined path merges. Several roots
        # (Windows drives) leave a relative name ambiguous.
        # https://github.com/coverlet-coverage/coverlet/blob/master/src/coverlet.core/Reporters/CoberturaReporter.cs
        roots = [element.text or "" for element in root.findall("./sources/source")]
        if len(roots) > 1:
            raise CoverageError(f"{report.name} has several source roots, so its file names are ambiguous.")

        for package in packages:
            lines = merged.setdefault(package.get("name", ""), {})
            for element in package.findall("./classes/class"):
                source = source_key("".join(roots) + element.get("filename", ""))
                # WHY: Only the class's direct lines count; method details can repeat an uneven subset (MySql).
                for line in element.findall("./lines/line"):
                    key = (source, element.get("name", ""), int(line.get("number", "0")))
                    entry = lines.setdefault(key, Line())
                    entry.covered |= int(line.get("hits", "0")) > 0
                    condition = line.get("condition-coverage")
                    if condition is not None:
                        match = CONDITION.search(condition)
                        if match is None or not 0 < int(match[2]) or int(match[1]) > int(match[2]):
                            raise CoverageError(f"{report.name} has an invalid condition-coverage value.")

                        # WHY: The same line from several test projects merges conservatively (SafeMigrations).
                        entry.covered_branches = max(entry.covered_branches, int(match[1]))
                        entry.total_branches = max(entry.total_branches, int(match[2]))

    return merged


def measure(label: str, lines: list[Line], line_floor: float, branch_floor: float | None) -> tuple[str, list[str]]:
    """Return a summary line and the errors of one threshold."""
    covered = sum(line.covered for line in lines)
    branches = sum(line.covered_branches for line in lines)
    total = sum(line.total_branches for line in lines)
    line_percent = 100 * covered / len(lines) if lines else 0.0
    branch_percent = 100 * branches / total if total else 0.0
    errors = []
    if not lines:
        errors.append(f"{label} has no instrumented lines.")
    elif line_percent < line_floor:
        errors.append(f"{label} line coverage {line_percent:.2f}% is below {line_floor:.2f}%.")

    if branch_floor is None:
        if total:
            errors.append(f"{label} has instrumented branches but declares no branch floor.")
    elif not total:
        errors.append(f"{label} has no instrumented branches.")
    elif branch_percent < branch_floor:
        errors.append(f"{label} branch coverage {branch_percent:.2f}% is below {branch_floor:.2f}%.")

    return (f"{label}: lines {covered}/{len(lines)} ({line_percent:.2f}%), branches {branches}/{total} "
            f"({branch_percent:.2f}%)"), errors


def verify(directory: Path, policy: dict, shipped: list[str], *, now: float | None = None) -> list[str]:
    """Check all thresholds; every shipped assembly must have a threshold and executed lines (NestedSet)."""
    merged = read_reports(directory, policy, time.time() if now is None else now)
    names = {entry["name"] for entry in policy["assemblies"]}
    unmeasured = sorted(set(shipped) - names)
    if unmeasured:
        raise CoverageError(f"Shipped assemblies without a coverage threshold: {', '.join(unmeasured)}.")

    summary, errors = [], []
    for entry in policy["assemblies"]:
        lines = list(merged.get(entry["name"], {}).values())
        if lines and not any(line.covered for line in lines):
            errors.append(f"{entry['name']} has no executed lines.")

        text, failures = measure(entry["name"], lines, entry["minimumLinePercent"], entry["minimumBranchPercent"])
        summary.append(text)
        errors.extend(failures)

    for entry in policy["criticalClasses"]:
        lines = [line for (source, name, number), line in merged.get(entry["assembly"], {}).items()
                 if name == entry["name"]]
        if not lines:
            errors.append(f"The reports have no class {entry['name']}.")
            continue

        text, failures = measure(f"critical class {entry['name']}", lines, entry["minimumLinePercent"],
                                 entry["minimumBranchPercent"])
        summary.append(text)
        errors.extend(failures)

    if errors:
        raise CoverageError(" ".join(errors))

    return summary


def main(argv: list[str] | None = None) -> int:
    """Run the gate from the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--release-policy", type=Path,
                        help="Release policy whose release.packages name the shipped assemblies.")
    args = parser.parse_args(argv)
    try:
        # WHY: The shipped assemblies are the release packages, so there is no second list to keep in sync.
        shipped = []
        if args.release_policy is not None and args.release_policy.is_file():
            shipped = json.loads(args.release_policy.read_text(encoding="utf-8"))["release"]["packages"]
        for line in verify(args.reports, load_policy(args.policy), shipped):
            print(line)
    except CoverageError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
