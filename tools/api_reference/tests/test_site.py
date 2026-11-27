import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.api_reference import __main__ as cli
from tools.api_reference import site

REPOSITORY = "doka-labs/Doka.Example"
EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "release-policy.json"

INDEX = """# Documentation

Intro text.

## Tutorials

| Document | Purpose |
| --- | --- |
| [Getting started](guides/getting-started.md) | First import and export |

## Reference

| Document | Purpose |
| --- | --- |
| [Limitations](limitations.md) | What is not supported |
"""

EXPLANATION = """
## Explanation

| Document | Purpose |
| --- | --- |
| [Architecture decisions](decisions/README.md) | Why the contracts are as they are |
"""


def record(number, status="implemented"):
    return {"id": f"D-{number:03d}", "title": f"Decision {number}", "status": status,
            "date": "2026-11-21", "path": f"D-{number:03d}-decision.md", "supersedes": [],
            "superseded_by": None, "amends": [], "amended_by": [], "depends_on": []}


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / ".github").mkdir()
        shutil.copyfile(EXAMPLE, self.root / ".github" / "release-policy.json")
        for package in ("Doka.Example", "Doka.Example.Extensions"):
            project = self.root / "src" / package / f"{package}.csproj"
            project.parent.mkdir(parents=True)
            project.write_text("<Project Sdk=\"Microsoft.NET.Sdk\" />\n", encoding="utf-8")
        (self.root / "docs" / "guides").mkdir(parents=True)
        (self.root / "docs" / "guides" / "getting-started.md").write_text("# Getting started\n", encoding="utf-8")
        (self.root / "docs" / "limitations.md").write_text("# Limitations\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Doka.Example\n", encoding="utf-8")
        self.index(INDEX)

    def tearDown(self):
        self.directory.cleanup()

    def index(self, text):
        (self.root / "docs" / "README.md").write_text(text, encoding="utf-8")

    def decisions(self, index):
        directory = self.root / "docs" / "decisions"
        directory.mkdir()
        (directory / "README.md").write_text("# Architecture decisions\n", encoding="utf-8")
        (directory / "index.md").write_text("# Decisions\n", encoding="utf-8")
        for item in index.get("decisions", []):
            (directory / item["path"]).write_text(f"# {item['id']}\n", encoding="utf-8")
        (directory / "index.json").write_text(json.dumps(index), encoding="utf-8")
        self.index(INDEX + EXPLANATION)

    def test_site_lists_the_kinds_in_fixed_order_and_the_api_reference(self):
        # Arrange
        expected = (
            'items:\n- name: Overview\n  href: index.md\n- name: "Tutorials"\n  items:\n'
            '  - name: "Getting started"\n    href: "docs/guides/getting-started.md"\n'
            '- name: "Reference"\n  items:\n  - name: "Limitations"\n    href: "docs/limitations.md"\n'
            '- name: API reference\n  href: api/\n')

        # Act
        result = site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertEqual(expected, (self.root / ".docfx" / "toc.yml").read_text(encoding="utf-8"))
        self.assertEqual(["Tutorials", "Reference"], result["sections"])
        self.assertEqual("# Doka.Example\n", (self.root / ".docfx" / "index.md").read_text(encoding="utf-8"))

    def test_config_builds_the_shipped_projects_with_the_modern_template(self):
        # Arrange
        expected = ["src/Doka.Example/Doka.Example.csproj",
                    "src/Doka.Example.Extensions/Doka.Example.Extensions.csproj"]

        # Act
        result = site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        config = json.loads((self.root / ".docfx" / "docfx.json").read_text(encoding="utf-8"))
        self.assertEqual(expected, result["projects"])
        self.assertEqual(expected, config["metadata"][0]["src"][0]["files"])
        self.assertEqual(["default", "modern"], config["build"]["template"])
        self.assertIn("docs/**/*.md", config["build"]["content"][0]["files"])

    def test_package_id_in_the_project_file_selects_the_project(self):
        # Arrange
        old = self.root / "src" / "Doka.Example.Extensions"
        shutil.rmtree(old)
        project = self.root / "src" / "Extensions" / "Extensions.csproj"
        project.parent.mkdir(parents=True)
        project.write_text("<Project><PropertyGroup><PackageId>Doka.Example.Extensions</PackageId>"
                           "</PropertyGroup></Project>\n", encoding="utf-8")

        # Act
        result = site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertEqual("src/Extensions/Extensions.csproj", result["projects"][1])

    def test_missing_project_of_a_package_fails(self):
        # Arrange
        shutil.rmtree(self.root / "src" / "Doka.Example.Extensions")

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertIn("No project under src/ packs Doka.Example.Extensions", str(raised.exception))

    def test_unknown_section_fails(self):
        # Arrange
        self.index(INDEX.replace("## Reference", "## Guides"))

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.navigation(self.root)

        # Assert
        self.assertIn("unknown section 'Guides'", str(raised.exception))

    def test_row_without_a_link_fails(self):
        # Arrange
        self.index(INDEX.replace("[Limitations](limitations.md)", "Limitations"))

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.navigation(self.root)

        # Assert
        self.assertIn("a row must be | [Title](path.md) | Purpose |", str(raised.exception))

    def test_link_to_a_missing_document_fails(self):
        # Arrange
        self.index(INDEX.replace("limitations.md", "missing.md"))

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.navigation(self.root)

        # Assert
        self.assertIn("'missing.md' does not exist", str(raised.exception))

    def test_link_leaving_docs_fails(self):
        # Arrange
        self.index(INDEX.replace("limitations.md", "../README.md"))

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.navigation(self.root)

        # Assert
        self.assertIn("leaves docs/", str(raised.exception))

    def test_table_without_the_fixed_header_fails(self):
        # Arrange
        self.index(INDEX.replace("| Document | Purpose |\n| --- | --- |\n| [Limitations]",
                                 "| Name | Purpose |\n| --- | --- |\n| [Limitations]"))

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.navigation(self.root)

        # Assert
        self.assertIn("must start with | Document | Purpose |", str(raised.exception))

    def test_decision_records_nest_below_the_decisions_row_sorted_by_id(self):
        # Arrange
        self.decisions({"schema_version": 1, "generator": "future field",
                        "decisions": [record(2, "superseded"), record(1), record(4, "deprecated"), record(3, "rejected")]})
        expected = (
            '- name: "Explanation"\n  items:\n'
            '  - name: "Architecture decisions"\n    href: "docs/decisions/README.md"\n    items:\n'
            '    - name: "Index"\n      href: "docs/decisions/index.md"\n'
            '    - name: "D-001 Decision 1"\n      href: "docs/decisions/D-001-decision.md"\n'
            '    - name: "D-002 Decision 2 (superseded)"\n      href: "docs/decisions/D-002-decision.md"\n'
            '    - name: "D-003 Decision 3 (rejected)"\n      href: "docs/decisions/D-003-decision.md"\n'
            '    - name: "D-004 Decision 4 (deprecated)"\n      href: "docs/decisions/D-004-decision.md"\n'
            '- name: API reference\n')

        # Act
        site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertIn(expected, (self.root / ".docfx" / "toc.yml").read_text(encoding="utf-8"))

    def test_decision_records_sort_by_their_number(self):
        # Arrange
        late = record(1000)
        self.decisions({"schema_version": 1, "decisions": [late, record(999)]})

        # Act
        items = site.decisions(self.root)

        # Assert
        self.assertEqual(["Index", "D-999 Decision 999", "D-1000 Decision 1000"], [item["name"] for item in items])

    def test_unknown_decision_index_schema_fails(self):
        # Arrange
        self.decisions({"schema_version": 2, "decisions": [record(1)]})

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertIn("schema_version must be 1", str(raised.exception))

    def test_decision_without_a_title_fails(self):
        # Arrange
        entry = record(1)
        del entry["title"]
        self.decisions({"schema_version": 1, "decisions": [entry]})

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertIn("decision 1 has no title", str(raised.exception))

    def test_decision_path_leaving_the_directory_fails(self):
        # Arrange
        entry = record(1)
        self.decisions({"schema_version": 1, "decisions": [entry]})
        entry["path"] = "../limitations.md"
        (self.root / "docs" / "decisions" / "index.json").write_text(
            json.dumps({"schema_version": 1, "decisions": [entry]}), encoding="utf-8")

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertIn("must name a Markdown file in docs/decisions", str(raised.exception))

    def test_missing_decision_index_fails(self):
        # Arrange
        self.decisions({"schema_version": 1, "decisions": [record(1)]})
        (self.root / "docs" / "decisions" / "index.json").unlink()

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertIn("index.json is missing", str(raised.exception))

    def test_decisions_without_the_explanation_row_fail(self):
        # Arrange
        self.decisions({"schema_version": 1, "decisions": [record(1)]})
        self.index(INDEX)

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertIn("must link decisions/README.md exactly once, under Explanation", str(raised.exception))

    def test_existing_stage_in_the_repository_fails(self):
        # Arrange
        (self.root / ".docfx").mkdir()

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertIn(".docfx already exists", str(raised.exception))

    def test_stage_links_other_repository_files_to_github_and_leaves_the_checkout(self):
        # Arrange
        (self.root / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (self.root / "README.md").write_text("[License](LICENSE) and [limits](docs/limitations.md)\n", encoding="utf-8")
        original = (self.root / "docs" / "limitations.md").read_text(encoding="utf-8")

        # Act
        site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        self.assertEqual("[License](https://github.com/doka-labs/Doka.Example/blob/v1.2.0/LICENSE) and "
                         "[limits](docs/limitations.md)\n",
                         (self.root / ".docfx" / "index.md").read_text(encoding="utf-8"))
        self.assertEqual(original, (self.root / ".docfx" / "docs" / "limitations.md").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "index.md").exists())
        self.assertEqual("[License](LICENSE) and [limits](docs/limitations.md)\n",
                         (self.root / "README.md").read_text(encoding="utf-8"))

    def test_config_reads_the_projects_from_the_checkout(self):
        # Arrange
        checkout = ".."

        # Act
        site.write(self.root, "Doka.Example", REPOSITORY, "v1.2.0")

        # Assert
        config = json.loads((self.root / ".docfx" / "docfx.json").read_text(encoding="utf-8"))
        self.assertEqual(checkout, config["metadata"][0]["src"][0]["src"])

    def test_tag_that_is_not_a_version_fails(self):
        # Arrange
        tag = "main"

        # Act
        with self.assertRaises(site.SiteError) as raised:
            site.write(self.root, "Doka.Example", REPOSITORY, tag)

        # Assert
        self.assertIn("'main' is not v<version>", str(raised.exception))


class DeployTests(unittest.TestCase):
    def test_stable_release_replaces_the_site(self):
        # Arrange
        releases = [published("v1.1.0", prerelease=False), published("v1.0.0", prerelease=False)]

        # Act
        decision = site.should_deploy("1.1.0", releases)

        # Assert
        self.assertTrue(decision)

    def test_prerelease_before_any_stable_release_replaces_the_site(self):
        # Arrange
        releases = [published("v1.0.0-rc.2", prerelease=True), published("v1.0.0-rc.1", prerelease=True),
                    {"tag_name": "v1.0.0", "draft": True, "prerelease": False}]

        # Act
        decision = site.should_deploy("1.0.0-rc.2", releases)

        # Assert
        self.assertTrue(decision)

    def test_prerelease_after_a_stable_release_keeps_the_site(self):
        # Arrange
        releases = [published("v2.0.0-rc.1", prerelease=True), published("v1.0.0", prerelease=False)]

        # Act
        decision = site.should_deploy("2.0.0-rc.1", releases)

        # Assert
        self.assertFalse(decision)

    def test_release_missing_from_the_list_keeps_the_site(self):
        # Arrange
        releases = [published("v1.0.0", prerelease=False)]

        # Act
        decision = site.should_deploy("1.1.0", releases)

        # Assert
        self.assertFalse(decision)

    def test_draft_release_keeps_the_site(self):
        # Arrange
        releases = [{"tag_name": "v1.0.0-rc.1", "draft": True, "prerelease": False, "immutable": False}]

        # Act
        decision = site.should_deploy("1.0.0-rc.1", releases)

        # Assert
        self.assertFalse(decision)

    def test_mutable_published_release_keeps_the_site(self):
        # Arrange
        releases = [{"tag_name": "v1.0.0", "draft": False, "prerelease": False, "immutable": False}]

        # Act
        decision = site.should_deploy("1.0.0", releases)

        # Assert
        self.assertFalse(decision)

    def test_maintenance_release_of_an_older_line_keeps_the_site(self):
        # Arrange
        releases = [published("v1.4.1", prerelease=False), published("v2.0.0", prerelease=False)]

        # Act
        decision = site.should_deploy("1.4.1", releases)

        # Assert
        self.assertFalse(decision)

    def test_lower_prerelease_before_any_stable_release_keeps_the_site(self):
        # Arrange
        releases = [published("v1.0.0-rc.1", prerelease=True), published("v1.0.0-rc.2", prerelease=True)]

        # Act
        decision = site.should_deploy("1.0.0-rc.1", releases)

        # Assert
        self.assertFalse(decision)

    def test_stable_release_after_a_mutable_older_stable_release_replaces_the_site(self):
        # Arrange
        releases = [published("v1.1.0", prerelease=False),
                    {"tag_name": "v1.0.0", "draft": False, "prerelease": False, "immutable": False},
                    {"tag_name": "nightly", "draft": False, "prerelease": True, "immutable": False}]

        # Act
        decision = site.should_deploy("1.1.0", releases)

        # Assert
        self.assertTrue(decision)

    def test_newer_draft_does_not_keep_the_site(self):
        # Arrange
        releases = [{"tag_name": "v2.0.0", "draft": True, "prerelease": False, "immutable": False},
                    published("v1.1.0", prerelease=False)]

        # Act
        decision = site.should_deploy("1.1.0", releases)

        # Assert
        self.assertTrue(decision)

class ReleaseListingTests(unittest.TestCase):
    def test_every_page_is_read_until_a_short_page(self):
        # Arrange
        pages = {1: [published(f"v1.0.{index}", prerelease=False) for index in range(100)],
                 2: [published("v2.0.0", prerelease=False)]}
        routes = []

        class Api:
            def request(self, route):
                routes.append(route)
                return pages[int(route.rsplit("=", 1)[1])]

        # Act
        releases = cli._releases(Api(), "doka-labs/Doka.Example")

        # Assert
        self.assertEqual(101, len(releases))
        self.assertEqual(["repos/doka-labs/Doka.Example/releases?per_page=100&page=1",
                          "repos/doka-labs/Doka.Example/releases?per_page=100&page=2"], routes)

def published(tag: str, *, prerelease: bool) -> dict:
    """Return a release entry as the list endpoint reports a published immutable release."""
    return {"tag_name": tag, "draft": False, "prerelease": prerelease, "immutable": True}


if __name__ == "__main__":
    unittest.main()
