"""Generate `.github/dependabot.yml` from a template and the active release lines.

The template `.github/dependabot-template.yml` holds one entry per ecosystem
and directory (schedule, cooldown, groups, own ignore rules) without
`target-branch`. The generator writes each entry once per active line with
that line's `target-branch` and adds the line boundaries:

- `dotnet-sdk`: no major updates.
- `nuget`, coupled toolchain: framework-coupled packages stay below the next
  major of the line. Independent toolchain: framework-coupled packages get no
  major updates at all; a new framework major arrives with its target framework.

Only the bounded template shape below is read; it is not a general YAML parser.
"""

from __future__ import annotations

import re

from tools.release_lines import policy as policies

HEADER = "version: 2\nupdates:\n"
GENERATED_NOTICE = ("# Generated from .github/dependabot-template.yml and .github/release-policy.json by\n"
                    "# doka-labs/workflows; edit those files instead.\n")
ENTRY = re.compile(r"^  - package-ecosystem: ([a-z-]+)\n", re.MULTILINE)
KEY = re.compile(r"^    ([a-z-]+):", re.MULTILINE)
# WHY: A line keeps its SDK major: the toolchain check requires the SDK major of the line's framework, and an SDK
# major update would fail it (the same SDK rule in the Dependabot files of all three repositories).
SDK_RULE = ('      - dependency-name: "*"\n'
            "        update-types:\n"
            "          - version-update:semver-major\n")


class DependabotError(RuntimeError):
    """The template, the generated file or a line boundary is invalid."""


def entries(template: str) -> list[tuple[str, str]]:
    """Split a template into (ecosystem, block) and require the canonical bounded shape."""
    if not template.startswith(HEADER):
        raise DependabotError("The Dependabot template must start with 'version: 2' and 'updates:'.")

    body = template[len(HEADER):]
    matches = list(ENTRY.finditer(body))
    if not matches or matches[0].start() != 0 or len(matches) != body.count("package-ecosystem:"):
        raise DependabotError("Every template entry must start with '  - package-ecosystem: <name>'.")

    result = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        block = body[match.start():end].rstrip("\n") + "\n"
        keys = KEY.findall(block)
        if len(keys) != len(set(keys)):
            raise DependabotError(f"Template entry {match[1]} repeats a key.")

        if "target-branch" in keys or "target-branch" in block:
            raise DependabotError("The template must not set target-branch; the generator sets it per line.")

        if "directory" not in keys and "directories" not in keys:
            raise DependabotError(f"Template entry {match[1]} needs directory or directories.")

        result.append((match[1], block))

    return result


def framework_rules(patterns: list[str], major: int, coupling: str) -> str:
    """Return the ignore rules that keep framework-coupled NuGet packages inside the line."""
    rules = ""
    for pattern in patterns:
        if coupling == "coupled":
            # WHY: ">=N.0.0-0" alone would admit prereleases like N.0.0--x: SemVer compares letter or hyphen
            # identifiers "lexically in ASCII sort order", and "-" (0x2D) sorts before "0" (0x30). The same pair
            # runs in NestedSet's generated dependabot.yml, whose last 40 Dependabot update runs succeeded.
            # https://semver.org/spec/v2.0.0.html
            rules += (f'      - dependency-name: "{pattern}"\n'
                      f'        versions: [">={major + 1}.0.0-0", ">={major + 1}.0.0--"]\n')
        else:
            rules += (f'      - dependency-name: "{pattern}"\n'
                      "        update-types:\n"
                      "          - version-update:semver-major\n")

    return rules


def _template_ignores(block: str) -> list[str]:
    """Return the dependency names of the template's own ignore rules."""
    return [name.strip().strip("\"'") for name in re.findall(r"^      - dependency-name: ([^\n]+)$", block,
                                                             re.MULTILINE)]


def with_rules(block: str, rules: str) -> str:
    """Add ignore rules to a block, after the template's own ignore rules when it has any."""
    lines = block.splitlines(keepends=True)
    try:
        start = next(index for index, line in enumerate(lines) if line == "    ignore:\n")
    except StopIteration:
        return block + "    ignore:\n" + rules

    end = start + 1
    while end < len(lines) and (lines[end].startswith("      ") or not lines[end].strip()):
        end += 1

    return "".join(lines[:end]) + rules + "".join(lines[end:])


def render(policy: dict, template: str) -> str:
    """Render the Dependabot configuration for all active lines."""
    validated = policies.validate_policy(policy)
    settings = validated["toolchain"]
    patterns = settings["frameworkPackages"] + settings["majorCeilingPackages"]
    blocks = entries(template)
    for ecosystem, block in blocks:
        if ecosystem in ("dotnet-sdk", "nuget"):
            names = _template_ignores(block)
            if ecosystem == "dotnet-sdk" and names:
                raise DependabotError("The dotnet-sdk template entry must not set ignore rules; the generator does.")

            clashing = [name for name in names if any(name.lower() == pattern.lower() for pattern in patterns)]
            if clashing:
                raise DependabotError(f"Template ignore rules for framework packages belong to the generator: "
                                      f"{', '.join(clashing)}.")

    lines = policies.lines_for(validated, "maintenance")
    if not lines:
        return GENERATED_NOTICE + "version: 2\nupdates: []\n"

    output = GENERATED_NOTICE + HEADER
    for line in lines:
        branch = line["sourceRef"].removeprefix("refs/heads/")
        for ecosystem, block in blocks:
            header, rest = block.split("\n", 1)
            entry = f"{header}\n    target-branch: {branch}\n{rest}"
            if ecosystem == "dotnet-sdk":
                entry = with_rules(entry, SDK_RULE)
            elif ecosystem == "nuget":
                entry = with_rules(entry, framework_rules(patterns, line["packageMajor"], settings["coupling"]))

            output += entry

    return output


def check(policy: dict, template: str, committed: str) -> None:
    """Require the committed configuration to equal the rendered one, byte for byte."""
    expected = render(policy, template)
    if committed != expected:
        raise DependabotError("dependabot.yml differs from the configuration rendered from the template and the "
                              "active release lines.")
