"""DocFX site of a product repository: API reference of the shipped packages plus the documents under docs/.

The navigation follows docs/README.md: one section per document kind (Tutorials, How-to guides, Reference,
Explanation), each a table whose first column links the document. Every Markdown file under docs/ is built, so
links between documents resolve even when a document is not listed; only listed documents appear in the
navigation. Nothing is written into the product checkout except the generated docfx.json, toc.yml and index.md.

Decision records under docs/decisions appear below the Explanation row that links decisions/README.md: first the
index that madr-check writes, then one entry per record from its index.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from tools.release_lines import policy as policies

KINDS = ("Tutorials", "How-to guides", "Reference", "Explanation")
HEADING = re.compile(r"^## (.+?)\s*$")
ROW_LINK = re.compile(r"^\|\s*\[([^\]]+)\]\(([^)\s]+)\)\s*\|(.*)\|\s*$")
TABLE_HEADER = re.compile(r"^\|\s*Document\s*\|\s*Purpose\s*\|\s*$")
TABLE_RULE = re.compile(r"^\|\s*:?-{3,}:?\s*\|\s*:?-{3,}:?\s*\|\s*$")
PACKAGE_ID = re.compile(r"<PackageId>\s*([^<\s]+)\s*</PackageId>")
DECISIONS = PurePosixPath("docs/decisions")
DECISIONS_README = DECISIONS / "README.md"
DECISION_INDEX_SCHEMA = 1
# WHY: A record with one of these states is not binding or no longer recommended, so the navigation says so.
FLAGGED_STATES = ("superseded", "rejected", "deprecated")


class SiteError(RuntimeError):
    """The repository does not provide what the site needs; the message names the file and the fix."""


def require(condition: bool, message: str) -> None:
    """Raise a site error when a condition does not hold."""
    if not condition:
        raise SiteError(message)


def navigation(root: Path) -> list[dict]:
    """Return the kinds of docs/README.md in their fixed order, each with its listed documents."""
    index = root / "docs" / "README.md"
    require(index.is_file(), "docs/README.md is missing.")
    sections: dict[str, list[dict]] = {}
    current = None
    expect = None
    for number, line in enumerate(index.read_text(encoding="utf-8").splitlines(), start=1):
        heading = HEADING.match(line)
        if heading:
            name = heading[1]
            require(name in KINDS, f"docs/README.md:{number}: unknown section '{name}'; use one of {', '.join(KINDS)}.")
            require(name not in sections, f"docs/README.md:{number}: section '{name}' appears twice.")
            current, expect = name, "header"
            sections[name] = []
            continue

        if current is None or not line.strip():
            continue

        if expect == "header":
            require(TABLE_HEADER.match(line) is not None,
                    f"docs/README.md:{number}: the table of '{current}' must start with | Document | Purpose |.")
            expect = "rule"
            continue

        if expect == "rule":
            require(TABLE_RULE.match(line) is not None, f"docs/README.md:{number}: the table rule is missing.")
            expect = "row"
            continue

        row = ROW_LINK.match(line)
        require(row is not None, f"docs/README.md:{number}: a row must be | [Title](path.md) | Purpose |.")
        href = row[2].split("#", 1)[0]
        require(href.endswith(".md") and not href.startswith(("/", "http:", "https:")),
                f"docs/README.md:{number}: '{row[2]}' must be a relative link to a Markdown file.")
        target = PurePosixPath("docs") / href
        require(".." not in target.parts, f"docs/README.md:{number}: '{row[2]}' leaves docs/.")
        require((root / target).is_file(), f"docs/README.md:{number}: '{row[2]}' does not exist.")
        require(bool(row[3].strip()), f"docs/README.md:{number}: the purpose of '{row[1]}' is empty.")
        sections[current].append({"name": row[1], "href": target.as_posix()})

    require(bool(sections), "docs/README.md has no section; use the document kinds as ## headings.")
    for name, items in sections.items():
        require(bool(items), f"docs/README.md: section '{name}' lists no document; remove it.")

    return [{"name": name, "items": sections[name]} for name in KINDS if name in sections]


def decisions(root: Path) -> list[dict]:
    """Return the decision index page and one entry per record from docs/decisions/index.json, sorted by id."""
    path = root / DECISIONS / "index.json"
    require(path.is_file(), f"{DECISIONS}/index.json is missing; madr-check index write creates it.")
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SiteError(f"{DECISIONS}/index.json is not JSON: {error}.") from error

    require(isinstance(index, dict) and index.get("schema_version") == DECISION_INDEX_SCHEMA,
            f"{DECISIONS}/index.json: schema_version must be {DECISION_INDEX_SCHEMA}.")
    records = index.get("decisions")
    require(isinstance(records, list) and bool(records), f"{DECISIONS}/index.json lists no decision.")
    require((root / DECISIONS / "index.md").is_file(), f"{DECISIONS}/index.md is missing.")
    items = []
    for number, record in enumerate(records, start=1):
        require(isinstance(record, dict), f"{DECISIONS}/index.json: decision {number} is not an object.")
        for field in ("id", "title", "status", "path"):
            require(isinstance(record.get(field), str) and bool(record[field].strip()),
                    f"{DECISIONS}/index.json: decision {number} has no {field}.")
        target = DECISIONS / record["path"]
        require(target.parent == DECISIONS and target.suffix == ".md",
                f"{DECISIONS}/index.json: '{record['path']}' must name a Markdown file in {DECISIONS}.")
        require((root / target).is_file(), f"{DECISIONS}/index.json: '{record['path']}' does not exist.")
        name = f"{record['id']} {record['title']}"
        if record["status"] in FLAGGED_STATES:
            name += f" ({record['status']})"
        items.append({"id": record["id"], "name": name, "href": target.as_posix()})

    ids = [item["id"] for item in items]
    require(len(set(ids)) == len(ids), f"{DECISIONS}/index.json lists an id twice.")
    items.sort(key=lambda item: item["id"])
    index_page = {"name": "Index", "href": (DECISIONS / "index.md").as_posix()}
    return [index_page] + [{"name": item["name"], "href": item["href"]} for item in items]


def attach_decisions(root: Path, sections: list[dict]) -> None:
    """Nest the decision records below the Explanation row that links decisions/README.md."""
    rows = [item for section in sections for item in section["items"] if item["href"] == DECISIONS_README.as_posix()]
    if not (root / DECISIONS).is_dir():
        return

    explanation = [item for section in sections if section["name"] == "Explanation" for item in section["items"]]
    require(len(rows) == 1 and rows[0] in explanation,
            f"{DECISIONS} exists; docs/README.md must link decisions/README.md exactly once, under Explanation.")
    rows[0]["items"] = decisions(root)


def projects(root: Path, packages: list[str]) -> list[str]:
    """Return the project file of each shipped package, relative to the root, in the order of the policy."""
    found: dict[str, str] = {}
    for project in sorted((root / "src").glob("*/*.csproj")):
        declared = PACKAGE_ID.search(project.read_text(encoding="utf-8"))
        package = declared[1] if declared else project.stem
        if package in packages:
            require(package not in found, f"Two projects pack {package}.")
            found[package] = project.relative_to(root).as_posix()

    missing = [package for package in packages if package not in found]
    require(not missing, f"No project under src/ packs {', '.join(missing)}.")
    return [found[package] for package in packages]


def yaml_string(value: str) -> str:
    """Return a YAML double-quoted scalar; JSON string escaping is valid YAML."""
    return json.dumps(value)


def toc(sections: list[dict]) -> str:
    """Return toc.yml: overview, the document kinds, and the API reference."""
    lines = ["items:", "- name: Overview", "  href: index.md"]
    for section in sections:
        lines += [f"- name: {yaml_string(section['name'])}", "  items:"]
        for item in section["items"]:
            lines += [f"  - name: {yaml_string(item['name'])}", f"    href: {yaml_string(item['href'])}"]
            if "items" in item:
                lines.append("    items:")
                for child in item["items"]:
                    lines += [f"    - name: {yaml_string(child['name'])}", f"      href: {yaml_string(child['href'])}"]

    lines += ["- name: API reference", "  href: api/"]
    return "\n".join(lines) + "\n"


def write(root: Path, title: str) -> dict:
    """Write docfx.json, toc.yml and index.md into the checkout and return the projects to restore."""
    policy = policies.load_policy(root)
    packages = list(policy["release"]["packages"])
    files = projects(root, packages)
    sections = navigation(root)
    attach_decisions(root, sections)
    readme = root / "README.md"
    require(readme.is_file(), "README.md is missing.")
    for name in ("docfx.json", "toc.yml", "index.md"):
        require(not (root / name).exists(), f"{name} already exists in the repository; the site generates it.")

    config = {
        "metadata": [{"src": [{"src": ".", "files": files}], "dest": "api"}],
        "build": {
            "content": [{"files": ["api/**.yml", "api/index.md", "toc.yml", "index.md", "docs/**/*.md"]}],
            "resource": [{"files": ["docs/**/*.png", "docs/**/*.svg"]}],
            "output": "_site",
            # WHY: Mermaid diagrams render only in the modern template.
            # https://dotnet.github.io/docfx/docs/markdown.html
            "template": ["default", "modern"],
            "globalMetadata": {"_appTitle": title, "_enableSearch": True},
        },
    }
    (root / "docfx.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (root / "toc.yml").write_text(toc(sections), encoding="utf-8")
    (root / "index.md").write_text(readme.read_text(encoding="utf-8"), encoding="utf-8")
    return {"projects": files, "sections": [section["name"] for section in sections]}


def should_deploy(version: str, releases: list[dict]) -> bool:
    """Return whether this release replaces the site: stable versions always, pre-releases only before any stable."""
    if not policies.parse_version(version).is_prerelease:
        return True

    return not any(not item.get("draft") and not item.get("prerelease") for item in releases)
