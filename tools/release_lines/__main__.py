"""Command line of the release-line tools: `python3 -m tools.release_lines <command>`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tools.dependency_audit.audit import AuditError
from tools.release_lines import attestation, coordinator, dependabot, guard, policy, qualification, toolchain
from tools.release_lines.github import GitHub, GitHubError, PacedGitHub


TEMPLATE_PATH = ".github/dependabot-template.yml"
DEPENDABOT_PATH = ".github/dependabot.yml"


def _read_text(path: Path) -> str:
    """Read a regular ASCII file of the checkout."""
    if path.is_symlink() or not path.is_file():
        raise dependabot.DependabotError(f"{path.name} must be a regular file.")

    try:
        return path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise dependabot.DependabotError(f"{path.name} is not readable ASCII text.") from error


def _write_summary(path: str | None, expected: str) -> None:
    """Show the expected dependabot.yml in the job summary so it can be copied."""
    if path:
        with open(path, "a", encoding="ascii") as stream:
            stream.write(f"## Expected {DEPENDABOT_PATH}\n\n```yaml\n{expected}```\n")


def _write_output(path: str | None, name: str, value: str) -> None:
    """Append one step output for GitHub Actions."""
    if path:
        with open(path, "a", encoding="ascii") as stream:
            stream.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit code."""
    parser = argparse.ArgumentParser(prog="python3 -m tools.release_lines")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Validate the release policy and the toolchain of a checkout.")
    check.add_argument("--root", type=Path, default=Path.cwd())
    check.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    check.add_argument("--line", required=True, help="Release line the checkout belongs to, as refs/heads/<name>.")
    check.add_argument("--pull-request", action="store_true",
                       help="Also require --line to be a registered line of the policy on protected main.")
    render = commands.add_parser("render-dependabot", help="Print the dependabot.yml for the active lines.")
    render.add_argument("--root", type=Path, default=Path.cwd())
    render.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    pr = commands.add_parser("pr-qualification", help="Write the subject and predicate of a PR qualification.")
    pr.add_argument("--root", type=Path, required=True)
    pr.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore-qualification", help="Restore the locked graph for release qualification.")
    restore.add_argument("--root", type=Path, required=True)
    qualify = commands.add_parser("qualify", help="Qualify a release candidate and write its predicates.")
    qualify.add_argument("--root", type=Path, required=True)
    qualify.add_argument("--version", required=True)
    qualify.add_argument("--qualification-only", choices=("true", "false"), required=True)
    qualify.add_argument("--trx", type=Path, required=True)
    qualify.add_argument("--output", type=Path, required=True)
    coordinate = commands.add_parser("coordinate", help="Dispatch a workload on active lines besides main.")
    coordinate.add_argument("--workload", required=True)
    coordinate.add_argument("--output", type=Path, required=True)
    run_guard = commands.add_parser("guard", help="Decide whether a workload run may proceed.")
    run_guard.add_argument("--maintenance", choices=("true", "false"), required=True)
    run_guard.add_argument("--correlation", default="")
    run_guard.add_argument("--source-sha", default="")
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    try:
        if args.command == "check":
            if args.pull_request:
                authority = coordinator.observe_authority(GitHub(), args.repository, allow_missing=True)
                policy.check_pr_base(authority["policy"], args.line)

            root = args.root.resolve()
            loaded = policy.load_policy(root, repository=args.repository)
            summary = toolchain.check(root, loaded, args.line)
            template = _read_text(root / TEMPLATE_PATH)
            expected = dependabot.render(loaded, template)
            if _read_text(root / DEPENDABOT_PATH) != expected:
                _write_summary(environment.get("GITHUB_STEP_SUMMARY"), expected)
                raise dependabot.DependabotError(
                    f"{DEPENDABOT_PATH} differs from the rendered configuration; the expected file is in the job "
                    "summary, or run render-dependabot locally.")
            print(f"Release policy {loaded['revision']} is valid; digest {policy.policy_digest(loaded)}.")
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0

        if args.command == "render-dependabot":
            root = args.root.resolve()
            loaded = policy.load_policy(root, repository=args.repository)
            print(dependabot.render(loaded, _read_text(root / TEMPLATE_PATH)), end="")
            return 0

        if args.command == "pr-qualification":
            api = GitHub()
            authority = coordinator.observe_authority(api, environment.get("GITHUB_REPOSITORY", ""))
            event = json.loads(Path(environment["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
            tree, predicate = qualification.pr_predicate(api, authority["policy"], environment, event,
                                                         args.root.resolve())
            subject = attestation.tree_subject(args.output, tree)
            attestation.write_predicate(args.output / "predicate.json", predicate)
            _write_output(environment.get("GITHUB_OUTPUT"), "subject", str(subject))
            _write_output(environment.get("GITHUB_OUTPUT"), "predicate", str(args.output / "predicate.json"))
            print(f"Pull request qualification prepared for tree {tree}.")
            return 0

        if args.command == "restore-qualification":
            qualification.restore_projects(args.root.resolve())
            return 0

        if args.command == "qualify":
            summary = qualification.qualify(GitHub(), environment, args.root.resolve(), version=args.version,
                                            qualification_only=args.qualification_only == "true",
                                            trx_dir=args.trx.resolve(), output=args.output.resolve())
            _write_output(environment.get("GITHUB_OUTPUT"), "subject", summary["subject"])
            _write_output(environment.get("GITHUB_OUTPUT"), "tree", summary["tree"])
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0

        if args.command == "coordinate":
            paced = PacedGitHub(GitHub(), requests_per_hour=coordinator.REQUESTS_PER_HOUR)
            report = coordinator.coordinate(paced, environment, args.workload, args.output)
            print(json.dumps(report, indent=2, sort_keys=True))
            if report["conclusion"] != "success":
                print("::error::At least one release line failed its workload; see the report.", file=sys.stderr)
                return 1

            return 0

        allowed = guard.decide(GitHub(), environment, maintenance=args.maintenance == "true",
                               correlation=args.correlation, source_sha=args.source_sha)
        _write_output(environment.get("GITHUB_OUTPUT"), "proceed", "true" if allowed else "false")
        print("Workload run proceeds." if allowed else "Main is not an active line; the scheduled run is skipped.")
        return 0
    except (policy.PolicyError, toolchain.ToolchainError, dependabot.DependabotError, qualification.QualificationError,
            AuditError, GitHubError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
