"""Command line of the release publication tools: `python3 -m tools.release_publication <command>`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tools.release_lines import attestation
from tools.release_lines.github import GitHub, GitHubError
from tools.release_lines.policy import PolicyError
from tools.release_publication import nuget, packages, publication


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
    parser = argparse.ArgumentParser(prog="python3 -m tools.release_publication")
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--root", type=Path, required=True)
    preflight.add_argument("--version", required=True)
    preflight.add_argument("--qualification-only", choices=("true", "false"), required=True)
    candidate = commands.add_parser("candidate")
    candidate.add_argument("--root", type=Path, required=True)
    candidate.add_argument("--version", required=True)
    candidate.add_argument("--packages", type=Path, required=True)
    candidate.add_argument("--probes", type=Path, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--candidate", type=Path, required=True)
    recheck = commands.add_parser("recheck")
    recheck.add_argument("--root", type=Path, required=True)
    recheck.add_argument("--candidate", type=Path, required=True)
    download = commands.add_parser("download-candidate")
    download.add_argument("--tag", required=True)
    download.add_argument("--output", type=Path, required=True)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("--root", type=Path, required=True)
    authorize.add_argument("--candidate", type=Path, required=True)
    authorize.add_argument("--output", type=Path, required=True)
    pending = commands.add_parser("pending-packages")
    pending.add_argument("--candidate", type=Path, required=True)
    pending.add_argument("--output", type=Path, required=True)
    readback = commands.add_parser("readback")
    readback.add_argument("--candidate", type=Path, required=True)
    readback.add_argument("--work", type=Path, required=True)
    release = commands.add_parser("publish-release")
    release.add_argument("--root", type=Path, required=True)
    release.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    api = GitHub()
    try:
        if args.command == "preflight":
            result = publication.preflight(api, environment, args.root.resolve(), version=args.version,
                                           qualification_only=args.qualification_only == "true")
            _output("resume", "true" if result["resume"] else "false")
            _output("tag", result["tag"])
            print(json.dumps(result, sort_keys=True))
            return 0

        if args.command == "candidate":
            manifest = publication.candidate(api, environment, args.root.resolve(), version=args.version,
                                             directory=args.packages.resolve(), probes=args.probes.resolve(),
                                             output=args.output.resolve())
            _output("packages", json.dumps([item["id"] for item in manifest["packages"]]))
            _summary(f"## Release candidate {manifest['version']}\n\nTag the qualified commit with an SSH-signed "
                     f"annotated tag, push it, then approve the publication:\n\n```sh\ngit tag -s {manifest['tag']} "
                     f"{manifest['commit']} -m \"{manifest['tag']}\"\ngit push origin {manifest['tag']}\n```\n")
            return 0

        if args.command == "stage":
            result = publication.stage(api, environment["GITHUB_REPOSITORY"], args.candidate.resolve())
            print(f"Draft {result['tag']} holds the candidate; uploaded {len(result['uploaded'])} assets.")
            return 0

        if args.command == "recheck":
            manifest = publication.authorize(api, environment, args.root.resolve(), args.candidate.resolve())
            print(f"Rechecked {manifest['tag']} before the next write.")
            return 0

        if args.command == "download-candidate":
            names = publication.download_candidate(api, environment["GITHUB_REPOSITORY"], args.tag,
                                                   args.output.resolve())
            print(f"Downloaded {len(names)} assets of {args.tag}.")
            return 0

        if args.command == "authorize":
            manifest = publication.authorize(api, environment, args.root.resolve(), args.candidate.resolve())
            attestation.write_predicate(args.output / "admission.json", publication.admission(manifest, environment))
            _output("admission", str(args.output / "admission.json"))
            print(f"Admitted {manifest['tag']} at {manifest['commit']}.")
            return 0

        manifest = publication.verify_candidate(environment["GITHUB_REPOSITORY"], args.candidate.resolve())
        if args.command == "pending-packages":
            states = nuget.availability(args.candidate.resolve(), manifest["packages"])
            absent = [item["id"] for item in states if item["state"] == "absent"]
            rows = [package for package in manifest["packages"] if package["id"] in absent]
            # WHY: Every primary package before every symbol package, each list in dependency order (NestedSet).
            args.output.write_text("".join(f"{row['nupkg']}\n" for row in rows)
                                   + "".join(f"{row['snupkg']}\n" for row in rows), encoding="ascii")
            print(f"{len(rows)} of {len(manifest['packages'])} packages still need to be pushed.")
            return 0

        if args.command == "readback":
            try:
                result = nuget.readback(args.candidate.resolve(), manifest["packages"], manifest["symbols"],
                                        args.work.resolve())
            except nuget.PendingError as error:
                # WHY: The packages are pushed; only nuget.org's indexing is late. The run ends without red and
                # without publishing the release; re-running this job continues here.
                _output("complete", "false")
                _summary(f"## Published to nuget.org, readback pending\n\n{error}\n\nRe-run the publish job to "
                         "finish the readback and the release.")
                print(f"::warning::{error}")
                return 0

            _output("complete", "true")
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        # WHY: Bytes, attestations, authority, environment and tag are checked again before the release is published
        # (F7 of all three repositories).
        manifest = publication.authorize(api, environment, args.root.resolve(), args.candidate.resolve())
        released = publication.publish_release(api, environment["GITHUB_REPOSITORY"], manifest, args.candidate.resolve())
        _summary(f"## Released {manifest['tag']}\n\n{released.get('html_url', '')}")
        return 0
    except (publication.PublicationError, packages.PackageError, nuget.NuGetError, PolicyError, GitHubError,
            OSError, ValueError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
