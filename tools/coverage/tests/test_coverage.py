import json
import tempfile
import unittest
from pathlib import Path

from tools.coverage import coverage

NOW = 1_800_000_000


def report(lines, assembly="Doka.Core", class_name="Doka.Core.Thing", source="/w/src/Core/Thing.cs", timestamp=NOW):
    body = "".join(f'<line number="{number}" hits="{hits}"'
                   + (f' condition-coverage="50% ({cov}/{tot})"' if tot else "") + " />"
                   for number, hits, cov, tot in lines)
    return (f'<?xml version="1.0"?><coverage timestamp="{timestamp}"><packages><package name="{assembly}"><classes>'
            f'<class name="{class_name}" filename="{source}"><lines>{body}</lines></class></classes></package>'
            "</packages></coverage>")


def policy(line=80, branch=50, critical=(), max_age=None):
    return {"schemaVersion": 1, "evidenceMaxAgeSeconds": max_age,
            "assemblies": [{"name": "Doka.Core", "minimumLinePercent": line, "minimumBranchPercent": branch}],
            "criticalClasses": [{"assembly": "Doka.Core", "name": name, "minimumLinePercent": floor,
                                 "minimumBranchPercent": None} for name, floor in critical]}


class CoverageTests(unittest.TestCase):
    def verify(self, reports, value, shipped=("Doka.Core",)):
        with tempfile.TemporaryDirectory() as directory:
            for index, text in enumerate(reports):
                Path(directory, f"r{index}.cobertura.xml").write_text(text, encoding="utf-8")

            return coverage.verify(Path(directory), value, list(shipped), now=NOW)

    def failure(self, reports, value, shipped=("Doka.Core",)):
        with self.assertRaises(coverage.CoverageError) as raised:
            self.verify(reports, value, shipped)

        return str(raised.exception)

    def test_reports_from_two_projects_merge_by_line(self):
        # Arrange
        first = report([(1, 1, 1, 2), (2, 0, 0, 0)], source="/a/src/Core/Thing.cs")
        second = report([(1, 0, 2, 2), (2, 1, 0, 0)], source="/b/src/Core/Thing.cs")

        # Act
        summary = self.verify([first, second], policy(line=100, branch=100))

        # Assert
        self.assertEqual(["Doka.Core: lines 2/2 (100.00%), branches 2/2 (100.00%)"], summary)

    def test_line_floor_is_enforced(self):
        # Arrange
        reports = [report([(1, 1, 0, 0), (2, 0, 0, 0)])]

        # Act
        message = self.failure(reports, policy(line=80, branch=None))

        # Assert
        self.assertIn("line coverage 50.00% is below 80.00%", message)

    def test_branches_without_branch_floor_fail(self):
        # Arrange
        reports = [report([(1, 1, 1, 2)])]

        # Act
        message = self.failure(reports, policy(line=50, branch=None))

        # Assert
        self.assertIn("declares no branch floor", message)

    def test_empty_report_fails(self):
        # Arrange
        reports = [report([(1, 1, 0, 0)]), '<?xml version="1.0"?><coverage><packages /></coverage>']

        # Act
        message = self.failure(reports, policy(line=50, branch=None))

        # Assert
        self.assertIn("empty or invalid Cobertura report", message)

    def test_shipped_assembly_without_threshold_fails(self):
        # Arrange
        reports = [report([(1, 1, 0, 0)])]

        # Act
        message = self.failure(reports, policy(line=50, branch=None), shipped=("Doka.Core", "Doka.Other"))

        # Assert
        self.assertIn("without a coverage threshold: Doka.Other", message)

    def test_assembly_without_executed_lines_fails(self):
        # Arrange
        reports = [report([(1, 0, 0, 0)])]

        # Act
        message = self.failure(reports, policy(line=1, branch=None))

        # Assert
        self.assertIn("has no executed lines", message)

    def test_critical_class_floor_is_enforced(self):
        # Arrange
        reports = [report([(1, 1, 0, 0), (2, 1, 0, 0), (3, 0, 0, 0)])]

        # Act
        message = self.failure(reports, policy(line=50, branch=None, critical=[("Doka.Core.Thing", 98)]))

        # Assert
        self.assertIn("critical class Doka.Core.Thing line coverage 66.67% is below 98.00%", message)

    def test_old_report_fails_when_age_is_bounded(self):
        # Arrange
        reports = [report([(1, 1, 0, 0)], timestamp=NOW - 7200)]

        # Act
        message = self.failure(reports, policy(line=50, branch=None, max_age=3600))

        # Assert
        self.assertIn("older than 3600 seconds", message)

    def test_future_report_fails(self):
        # Arrange
        reports = [report([(1, 1, 0, 0)], timestamp=NOW + 600)]

        # Act
        message = self.failure(reports, policy(line=50, branch=None, max_age=3600))

        # Assert
        self.assertIn("no valid timestamp", message)


class PolicyTests(unittest.TestCase):
    def load(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "coverage-policy.json")
            path.write_text(json.dumps(value), encoding="utf-8")
            return coverage.load_policy(path)

    def test_zero_branch_floor_fails(self):
        # Arrange
        value = policy(line=80, branch=0)

        # Act
        with self.assertRaises(coverage.CoverageError) as raised:
            self.load(value)

        # Assert
        self.assertIn("null or above 0", str(raised.exception))

    def test_critical_class_of_unknown_assembly_fails(self):
        # Arrange
        value = policy()
        value["criticalClasses"] = [{"assembly": "Other", "name": "X", "minimumLinePercent": 90,
                                     "minimumBranchPercent": None}]

        # Act
        with self.assertRaises(coverage.CoverageError) as raised:
            self.load(value)

        # Assert
        self.assertIn("belong to a policy assembly", str(raised.exception))


class ConditionValueTests(unittest.TestCase):
    def test_more_covered_than_total_branches_fails(self):
        # Arrange
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        Path(directory.name, "r.cobertura.xml").write_text(report([(1, 1, 2, 1)]), encoding="utf-8")

        # Act
        with self.assertRaises(coverage.CoverageError) as raised:
            coverage.verify(Path(directory.name), policy(), ["Doka.Core"], now=NOW)

        # Assert
        self.assertIn("invalid condition-coverage", str(raised.exception))


class MethodOnlyReportTests(unittest.TestCase):
    def test_report_with_method_lines_only_counts_as_empty(self):
        # Arrange
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        Path(directory.name, "a.cobertura.xml").write_text(report([(1, 1, 0, 0)]), encoding="utf-8")
        method_only = report([(1, 1, 0, 0)]).replace("<lines>", "<methods><method><lines>", 1).replace(
            "</lines>", "</lines></method></methods><lines></lines>", 1)
        Path(directory.name, "b.cobertura.xml").write_text(method_only, encoding="utf-8")

        # Act
        with self.assertRaises(coverage.CoverageError) as raised:
            coverage.verify(Path(directory.name), policy(branch=None), ["Doka.Core"], now=NOW)

        # Assert
        self.assertIn("b.cobertura.xml is an empty or invalid Cobertura report", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
