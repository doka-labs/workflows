import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.dependency_audit import audit

SOURCE = "https://api.nuget.org/v3/index.json"


def clean_report(paths):
    return {"version": 1, "parameters": "--vulnerable --include-transitive", "sources": [SOURCE],
            "projects": [{"path": str(path)} for path in paths]}


def package(identifier="Example", version="1.0.0", vulnerabilities=None):
    return {"id": identifier, "resolvedVersion": version, "vulnerabilities": vulnerabilities or []}


class ValidateReportTests(unittest.TestCase):
    def setUp(self):
        self.paths = [Path("/repo/src/A/A.csproj"), Path("/repo/tests/B/B.csproj")]

    def test_clean_report_without_frameworks_passes(self):
        # Arrange
        report = clean_report(self.paths)

        # Act
        count = audit.validate_report(report, self.paths)

        # Assert
        self.assertEqual(2, count)

    def test_vulnerable_transitive_package_fails(self):
        # Arrange
        report = clean_report(self.paths)
        report["projects"][0]["frameworks"] = [{"framework": "net10.0", "transitivePackages": [
            package("System.Text.Json", "8.0.0", [{"severity": "High", "advisoryurl": "https://example.test"}])]}]

        # Act
        with self.assertRaises(audit.AuditError) as raised:
            audit.validate_report(report, self.paths)

        # Assert
        self.assertIn("System.Text.Json 8.0.0", str(raised.exception))

    def test_missing_transitive_flag_fails(self):
        # Arrange
        report = clean_report(self.paths)
        report["parameters"] = "--vulnerable"

        # Act
        with self.assertRaises(audit.AuditError) as raised:
            audit.validate_report(report, self.paths)

        # Assert
        self.assertIn("--include-transitive", str(raised.exception))

    def test_other_source_fails(self):
        # Arrange
        report = clean_report(self.paths)
        report["sources"] = [SOURCE, "https://example.test/v3/index.json"]

        # Act
        with self.assertRaises(audit.AuditError) as raised:
            audit.validate_report(report, self.paths)

        # Assert
        self.assertIn("only source", str(raised.exception))

    def test_reported_problem_fails(self):
        # Arrange
        report = clean_report(self.paths)
        report["problems"] = [{"text": "Advisory source unavailable"}]

        # Act
        with self.assertRaises(audit.AuditError) as raised:
            audit.validate_report(report, self.paths)

        # Assert
        self.assertIn("problems", str(raised.exception))

    def test_missing_project_fails(self):
        # Arrange
        report = clean_report(self.paths[:1])

        # Act
        with self.assertRaises(audit.AuditError) as raised:
            audit.validate_report(report, self.paths)

        # Assert
        self.assertIn("exactly the project files", str(raised.exception))

    def test_repeated_package_fails(self):
        # Arrange
        report = clean_report(self.paths)
        report["projects"][0]["frameworks"] = [{"framework": "net10.0", "topLevelPackages": [package(), package()]}]

        # Act
        with self.assertRaises(audit.AuditError) as raised:
            audit.validate_report(report, self.paths)

        # Assert
        self.assertIn("incomplete or repeated", str(raised.exception))

    def test_package_without_vulnerability_list_fails(self):
        # Arrange
        report = clean_report(self.paths)
        incomplete = package()
        del incomplete["vulnerabilities"]
        report["projects"][0]["frameworks"] = [{"framework": "net10.0", "topLevelPackages": [incomplete]}]

        # Act
        with self.assertRaises(audit.AuditError) as raised:
            audit.validate_report(copy.deepcopy(report), self.paths)

        # Assert
        self.assertIn("incomplete or repeated", str(raised.exception))


class SourceProjectTests(unittest.TestCase):
    def test_build_output_and_hidden_directories_are_ignored(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("src/A/A.csproj", "src/A/bin/Copy.csproj", "src/A/obj/Copy.csproj",
                             ".workflows/tools/Helper.csproj", "examples/E/E.csproj"):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text("<Project />", encoding="utf-8")

            # Act
            projects = [path.relative_to(root.resolve()).as_posix() for path in audit.source_projects(root)]

        # Assert
        self.assertEqual(["examples/E/E.csproj", "src/A/A.csproj"], projects)

    def test_repository_without_projects_fails(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            # Act
            with self.assertRaises(audit.AuditError) as raised:
                audit.source_projects(Path(directory))

        # Assert
        self.assertIn("no project file", str(raised.exception))


class CacheLocationTests(unittest.TestCase):
    def test_cache_inside_evidence_fails(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence"
            cache = evidence / "http-cache"
            cache.mkdir(parents=True)

            # Act
            with self.assertRaises(audit.AuditError) as raised:
                audit.require_outside(cache, evidence)

        # Assert
        self.assertIn("outside", str(raised.exception))

    def test_cache_beside_evidence_passes(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence"
            cache = Path(directory) / "cache"
            evidence.mkdir()
            cache.mkdir()

            # Act
            result = audit.require_outside(cache, evidence)

        # Assert
        self.assertIsNone(result)


class AuditSolutionTests(unittest.TestCase):
    def test_xml_special_characters_in_a_path_stay_valid(self):
        # Arrange
        import xml.etree.ElementTree as ElementTree

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        project = root / "src" / "R&D <x>" / "A.csproj"

        # Act
        audit.write_audit_solution(root / "audit.slnx", [project])

        # Assert
        element = ElementTree.parse(root / "audit.slnx").getroot().find("Project")
        self.assertEqual("src/R&D <x>/A.csproj", element.get("Path"))


DATA = Path(__file__).parent / "data"
MEASURED_PROJECTS = [Path("/work/src/Consumer/Consumer.csproj").resolve(),
                     Path("/work/src/Direct/Direct.csproj").resolve()]


class MeasuredListingTests(unittest.TestCase):
    def listing(self, name):
        return json.loads((DATA / name).read_text(encoding="utf-8"))

    def test_clean_listing_of_sdk_10_passes(self):
        # Arrange
        report = self.listing("package-list-clean-sdk10.0.404.json")

        # Act
        count = audit.validate_report(report, MEASURED_PROJECTS)

        # Assert
        self.assertEqual(2, count)

    def test_clean_listing_of_sdk_11_passes(self):
        # Arrange
        report = self.listing("package-list-clean-sdk11.0.100.json")

        # Act
        count = audit.validate_report(report, MEASURED_PROJECTS)

        # Assert
        self.assertEqual(2, count)

    def test_vulnerable_listing_of_sdk_10_fails(self):
        # Arrange
        report = self.listing("package-list-vulnerable-sdk10.0.404.json")

        # Act
        with self.assertRaises(audit.AuditError) as raised:
            audit.validate_report(report, MEASURED_PROJECTS)

        # Assert
        self.assertIn("System.Text.Json 8.0.0", str(raised.exception))

    def test_vulnerable_listing_of_sdk_11_fails(self):
        # Arrange
        report = self.listing("package-list-vulnerable-sdk11.0.100.json")

        # Act
        with self.assertRaises(audit.AuditError) as raised:
            audit.validate_report(report, MEASURED_PROJECTS)

        # Assert
        self.assertIn("System.Text.Json 8.0.0", str(raised.exception))

    def test_vulnerable_transitive_package_alone_fails(self):
        # Arrange
        report = self.listing("package-list-vulnerable-sdk11.0.100.json")
        report["projects"] = [project for project in report["projects"] if project["path"].endswith("Consumer.csproj")]

        # Act
        with self.assertRaises(audit.AuditError) as raised:
            audit.validate_report(report, MEASURED_PROJECTS[:1])

        # Assert
        self.assertIn("System.Text.Json 8.0.0 in /work/src/Consumer/Consumer.csproj", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
