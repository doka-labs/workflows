"""Command line of the API reference site: `python3 -m tools.api_reference <command>`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from tools.api_reference import site
from tools.release_lines.github import GitHub, GitHubError
from tools.release_lines.policy import PolicyError


def _output(name: str, value: str) -> None:
    """Append one step output for GitHub Actions."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="ascii") as stream:
            stream.write(f"{name}={value}\n")


def _releases(api, repository: str) -> list[dict]:
    """Return every release the token can list, page by page."""
    # WHY: The list endpoint returns at most 100 releases per page, and the highest stable version can lie on any
    # page. https://docs.github.com/en/rest/releases/releases#list-releases
    found = []
    for page in range(1, 101):
        entries = api.request(f"repos/{repository}/releases?per_page=100&page={page}")
        if not isinstance(entries, list):
            raise site.SiteError("The release listing is invalid.")

        found.extend(entries)
        if len(entries) < 100:
            return found

    raise site.SiteError("The release listing has more than 10000 entries.")


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit code."""
    parser = argparse.ArgumentParser(prog="python3 -m tools.api_reference")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="Write the site sources into .docfx/ of the checkout.")
    generate.add_argument("--root", type=Path, required=True)
    generate.add_argument("--title", required=True)
    generate.add_argument("--repository", required=True, help="owner/name; links to other files go to its GitHub.")
    generate.add_argument("--tag", required=True, help="Released tag v<version>; links to other files name it.")
    generate.add_argument("--pack-configuration", default="Release", help="MSBuild configuration of the pack.")
    deploy = commands.add_parser("should-deploy", help="Decide whether the release replaces the published site.")
    deploy.add_argument("--repository", required=True)
    deploy.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            result = site.write(args.root.resolve(), args.title, args.repository, args.tag,
                                configuration=args.pack_configuration)
            _output("projects", " ".join(result["projects"]))
            print(f"Site for {', '.join(result['projects'])}; sections: {', '.join(result['sections'])}.")
            return 0

        releases = _releases(GitHub(), args.repository)
        decision = site.should_deploy(args.version, releases)
        _output("deploy", "true" if decision else "false")
        print(f"{args.version}: {'replaces' if decision else 'does not replace'} the published site.")
        return 0
    except (site.SiteError, PolicyError, GitHubError, OSError, ValueError, KeyError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
