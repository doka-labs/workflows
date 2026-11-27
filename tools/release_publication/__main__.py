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
from tools.release_publication import consumers, nuget, packages, publication, self_release


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


def _check_lines(result: dict) -> list[str]:
    """Return one aligned OK or FAIL line per check."""
    width = max(len(item["subject"]) for item in result["checks"])
    return [f"{'OK' if item['ok'] else 'FAIL':<5} {item['subject']:<{width}}  {item['detail']}".rstrip()
            for item in result["checks"]]


def _tag_report(result: dict) -> str:
    """Return the Markdown summary of the tag check in the release run: one line per check and the next step."""
    lines = [f"## Tag check for {result['tag']}", "", "```text", *_check_lines(result), "```", ""]
    total = len(result["checks"])
    if result["failures"]:
        lines.append(f"{result['failures']} of {total} checks failed. Fix them and run the candidate again; do not "
                     "create the tag.")
    else:
        lines.append(f"All {total} checks passed. Create, verify and push the signed tag {result['tag']} on "
                     f"{result['commit']} as the release runbook describes, then approve the waiting publication.")

    return "\n".join(lines) + "\n"


def _pre_tag_report(result: dict) -> str:
    """Return the plain text report of the local check before the candidate starts."""
    lines = [f"Pre-tag check for {result['tag']}", "", *_check_lines(result), ""]
    total = len(result["checks"])
    if result["failures"]:
        lines.append(f"{result['failures']} of {total} checks failed. Fix them before you start the candidate.")
    else:
        lines.append(f"All {total} checks passed. Start the release candidate for {result['tag']} on "
                     f"{result['commit']}.")

    return "\n".join(lines) + "\n"


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
    check = commands.add_parser("package-check", help="Check pull request packages like a candidate; no writes.")
    check.add_argument("--root", type=Path, required=True)
    check.add_argument("--version", required=True)
    check.add_argument("--packages", type=Path, required=True)
    check.add_argument("--probes", type=Path, required=True)
    check.add_argument("--tool", type=Path, required=True)
    check.add_argument("--supplier", required=True)
    check.add_argument("--output", type=Path, required=True)
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
    pre_tag = commands.add_parser(
        "pre-tag", help="Check locally, before the candidate starts, that the checkout can be released; no writes.")
    pre_tag.add_argument("--root", type=Path, required=True, help="Checkout of the product repository.")
    pre_tag.add_argument("--version", required=True, help="Version to release, without the leading v.")
    readiness = commands.add_parser(
        "tag-readiness", help="Check in the release run that the candidate commit can be tagged; no writes.")
    readiness.add_argument("--root", type=Path, required=True, help="Checkout of the product repository.")
    readiness.add_argument("--version", required=True, help="Version to tag, without the leading v.")
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
    pending.add_argument("--symbols-output", type=Path, required=True,
                         help="Symbol packages whose PDBs are not served yet.")
    readback = commands.add_parser("readback")
    readback.add_argument("--candidate", type=Path, required=True)
    readback.add_argument("--work", type=Path, required=True)
    readback.add_argument("--root", type=Path, required=True, help="Product checkout whose global.json is used.")
    own = commands.add_parser("self-release", help="Publish this repository's release of a signed version tag.")
    own.add_argument("--root", type=Path, required=True)
    own.add_argument("--tag", required=True)
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

        if args.command == "package-check":
            root, directory = args.root.resolve(), args.packages.resolve()
            repository = environment["GITHUB_REPOSITORY"]
            policy = observe_authority(api, repository)["policy"]
            inventory, _ = publication.check_packages(policy, root, directory, args.version, repository,
                                                      environment["GITHUB_SHA"], args.probes.resolve())
            # WHY: The same isolated consumers and SBOM checks as the candidate: the candidate bytes restore
            # unchanged, no test or build tooling leaks into the graph, and every SBOM validates.
            result = consumers.generate(directory, inventory, args.version, args.tool.resolve(),
                                        root / "global.json", repository, args.supplier, args.output.resolve())
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
            _summary(f"## Release candidate {manifest['version']}\n\nThe draft holds the candidate. The tag check "
                     "after the SBOM jobs decides whether the signed tag may be created.\n")
            return 0

        if args.command == "sbom-extract":
            path = publication.extract_sbom(args.handoff.resolve(), args.package, args.packages.resolve())
            print(f"Wrote {path.name}.")
            return 0

        if args.command == "pre-tag":
            result = publication.pre_tag(api, args.root.resolve(), args.version)
            print(_pre_tag_report(result), end="")
            return 1 if result["failures"] else 0

        if args.command == "tag-readiness":
            result = publication.tag_readiness(api, environment, args.root.resolve(), args.version)
            report = _tag_report(result)
            print(report)
            _summary(report)
            return 1 if result["failures"] else 0

        if args.command == "stage":
            result = publication.stage(api, environment["GITHUB_REPOSITORY"], args.candidate.resolve())
            print(f"Draft {result['tag']} holds the candidate; uploaded {len(result['uploaded'])} assets.")
            return 0

        if args.command == "recheck":
            directory = args.candidate.resolve()
            manifest = publication.authorize(api, environment, args.root.resolve(), directory)
            publication.check_publish_target(api, environment["GITHUB_REPOSITORY"], manifest, directory)
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

        # WHY: self-release has no candidate, so it runs before the candidate check of the commands below.
        if args.command == "self-release":
            result = self_release.release(api, args.root.resolve(), environment["GITHUB_REPOSITORY"], args.tag)
            _summary(f"## Released {result['tag']}\n\n{result['url']}")
            print(f"Released {result['tag']} at {result['commit']}: {result['url']}")
            return 0

        manifest = publication.verify_candidate(environment["GITHUB_REPOSITORY"], args.candidate.resolve())
        if args.command == "pending-packages":
            states = nuget.availability(args.candidate.resolve(), manifest["packages"])
            absent = [item["id"] for item in states if item["state"] == "absent"]
            rows = [package for package in manifest["packages"] if package["id"] in absent]
            # WHY: Every primary package before every symbol package, each list in dependency order (NestedSet).
            # A symbol package is listed while its Portable PDBs are not served yet, also when its primary is already
            # on nuget.org, so an attempt that stopped between the two pushes still delivers it. One whose PDBs are
            # served is left out: nuget.org would accept it again and replace the available one (MySql's preflight
            # pushes only missing symbols and requests a key only when anything is missing).
            symbols = nuget.pending_symbols(manifest["packages"], manifest["symbols"])
            args.output.write_text("".join(f"{row['nupkg']}\n" for row in rows), encoding="ascii")
            args.symbols_output.write_text("".join(f"{row['snupkg']}\n" for row in symbols), encoding="ascii")
            _output("push", "true" if rows or symbols else "false")
            print(f"{len(rows)} of {len(manifest['packages'])} primary packages still need to be pushed.")
            print(f"{len(symbols)} of {len(manifest['packages'])} symbol packages still need to be pushed.")
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
