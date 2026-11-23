"""Command line of the change-scope tools: `python3 -m tools.change_scope <command>`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tools.change_scope import scope


def _output(name: str, value: str) -> None:
    """Append one step output for GitHub Actions."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="ascii") as stream:
            stream.write(f"{name}={value}\n")


def _summary(text: str) -> None:
    """Append Markdown to the job summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit code."""
    parser = argparse.ArgumentParser(prog="python3 -m tools.change_scope")
    commands = parser.add_subparsers(dest="command", required=True)
    classify = commands.add_parser("classify", help="Write scope=docs or scope=full for a pull request.")
    classify.add_argument("--root", type=Path, required=True)
    classify.add_argument("--base", default="", help="Base commit; empty for every event but a pull request.")
    classify.add_argument("--head", default="")
    gate = commands.add_parser("gate", help="Judge the job results in GATE_RESULTS against the scope.")
    gate.add_argument("--scope", required=True)
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    try:
        if args.command == "classify":
            # WHY: Only a pull request has a base to compare with; pushes, schedules, dispatches and releases
            # always run everything.
            if not args.base:
                result = {"scope": "full", "paths": 0, "nonDocumentation": [], "reason": "no pull request base"}
            else:
                result = scope.classify(args.root.resolve(), args.base, args.head)

            _output("scope", result["scope"])
            _summary(f"## Change scope: {result['scope']}\n\n{result['paths']} changed paths.")
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        try:
            results = json.loads(environment.get("GATE_RESULTS", ""))
            skippable = json.loads(environment.get("GATE_SKIPPABLE", "[]"))
        except ValueError as error:
            raise scope.ScopeError("GATE_RESULTS and GATE_SKIPPABLE must be JSON.") from error

        if not isinstance(skippable, list) or any(not isinstance(item, str) for item in skippable):
            raise scope.ScopeError("GATE_SKIPPABLE must be a JSON list of job names.")

        problems = scope.judge(results, args.scope, skippable)
        _summary("## Required gate\n\n| Job | Result |\n| --- | --- |\n" + "".join(
            f"| {name} | {entry.get('result') if isinstance(entry, dict) else entry} |\n"
            for name, entry in sorted(results.items())) if isinstance(results, dict) else "")
        if problems:
            raise scope.ScopeError(" ".join(problems))

        print(f"Every job passed for a {args.scope} change.")
        return 0
    except scope.ScopeError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
