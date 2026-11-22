"""Command line of the release publication tools: `python3 -m tools.release_publication <command>`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tools.release_lines import attestation
from tools.release_lines.coordinator import observe_authority
from tools.release_lines.toolchain import target_frameworks
from tools.release_lines.github import GitHub, GitHubError
from tools.release_lines.policy import PolicyError
from tools.release_publication import consumers, nuget, packages, publication


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
    sbom = commands.add_parser("sbom")
    sbom.add_argument("--root", type=Path, required=True)
    sbom.add_argument("--version", required=True)
    sbom.add_argument("--packages", type=Path, required=True)
    sbom.add_argument("--tool", type=Path, required=True)
    sbom.add_argument("--supplier", required=True)
    sbom.add_argument("--output", type=Path, required=True)
    public = commands.add_parser("public-consumers")
    public.add_argument("--root", type=Path, required=True)
    public.add_argument("--candidate", type=Path, required=True)
    public.add_argument("--output", type=Path, required=True)
    candidate = commands.add_parser("candidate")
    candidate.add_argument("--root", type=Path, required=True)
    candidate.add_argument("--version", required=True)
    candidate.add_argument("--packages", type=Path, required=True)
    candidate.add_argument("--probes", type=Path, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    candidate.add_argument("--handoff", type=Path, required=True,
                           help="JSON file that carries the SBOMs to the SBOM jobs.")
    extract = commands.add_parser("sbom-extract", help="Write one package's SBOM from the candidate's handoff file.")
    extract.add_argument("--handoff", type=Path, required=True)
    extract.add_argument("--package", required=True)
    extract.add_argument("--packages", type=Path, required=True)
    pre_tag = commands.add_parser("pre-tag", help="Check locally that the checkout can be tagged; no writes.")
    pre_tag.add_argument("--root", type=Path, default=Path.cwd())
    pre_tag.add_argument("--version", required=True)
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
    pending.add_argument("--output", type=Path, required=True, help="Primary packages nuget.org does not hold.")
    pending.add_argument("--symbols-output", type=Path, required=True, help="Every symbol package.")
    readback = commands.add_parser("readback")
    readback.add_argument("--candidate", type=Path, required=True)
    readback.add_argument("--work", type=Path, required=True)
    readback.add_argument("--root", type=Path, required=True, help="Product checkout whose global.json is used.")
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

        if args.command == "sbom":
            root = args.root.resolve()
            policy = observe_authority(api, environment["GITHUB_REPOSITORY"])["policy"]

            inventory = packages.inventory(args.packages.resolve(), args.version, environment["GITHUB_REPOSITORY"],
                                           environment["GITHUB_SHA"],
                                           target_frameworks(root, policy["toolchain"]["coupling"]))
            result = consumers.generate(args.packages.resolve(), inventory, args.version, args.tool.resolve(),
                                        root / "global.json", environment["GITHUB_REPOSITORY"], args.supplier,
                                        args.output.resolve())
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        if args.command == "public-consumers":
            manifest = publication.read_json(args.candidate.resolve() / publication.CANDIDATE)
            result = consumers.verify_public(args.candidate.resolve(), manifest["packages"], manifest["version"],
                                             args.root.resolve() / "global.json", args.output.resolve())
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        if args.command == "candidate":
            manifest = publication.candidate(api, environment, args.root.resolve(), version=args.version,
                                             directory=args.packages.resolve(), probes=args.probes.resolve(),
                                             output=args.output.resolve())
            publication.write_sbom_handoff(manifest, args.output.resolve(), args.handoff.resolve())
            _output("packages", json.dumps([item["id"] for item in manifest["packages"]]))
            _summary(f"## Release candidate {manifest['version']}\n\nCheck locally with `python3 -E -s -m "
                     f"tools.release_publication pre-tag --version {manifest['version']}`, tag the qualified commit "
                     f"with an SSH-signed annotated tag, push the tag to origin, then approve the publication:"
                     f"\n\n```sh\ngit tag -s {manifest['tag']} {manifest['commit']} -m \"{manifest['tag']}\"\n```\n")
            return 0

        if args.command == "sbom-extract":
            path = publication.extract_sbom(args.handoff.resolve(), args.package, args.packages.resolve())
            print(f"Wrote {path.name}.")
            return 0

        if args.command == "pre-tag":
            result = publication.pre_tag(api, args.root.resolve(), args.version)
            print(f"Ready to tag {result['tag']} at {result['commit']}:\n  {result['command']}\n"
                  f"Then push the tag {result['tag']} to origin.")
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
            # Symbol packages are listed for every package, not only for pending primaries: an attempt that stopped
            # after a primary push would otherwise never push its symbols. NestedSet and SafeMigrations push all
            # symbol packages with --skip-duplicate on every publication run for the same reason.
            args.output.write_text("".join(f"{row['nupkg']}\n" for row in rows), encoding="ascii")
            args.symbols_output.write_text("".join(f"{row['snupkg']}\n" for row in manifest["packages"]),
                                           encoding="ascii")
            print(f"{len(rows)} of {len(manifest['packages'])} primary packages still need to be pushed.")
            return 0

        if args.command == "readback":
            try:
                result = nuget.readback(args.candidate.resolve(), manifest["packages"], manifest["symbols"],
                                        args.work.resolve(), args.root.resolve() / "global.json")
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
        # (MySql, SafeMigrations and NestedSet).
        manifest = publication.authorize(api, environment, args.root.resolve(), args.candidate.resolve())
        released = publication.publish_release(api, environment["GITHUB_REPOSITORY"], manifest,
                                               args.candidate.resolve())
        _summary(f"## Released {manifest['tag']}\n\n{released.get('html_url', '')}")
        return 0
    except (publication.PublicationError, packages.PackageError, nuget.NuGetError, consumers.ConsumerError,
            PolicyError, GitHubError, OSError, ValueError, KeyError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
