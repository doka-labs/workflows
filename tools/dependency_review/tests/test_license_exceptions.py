import json
import tempfile
import unittest
from pathlib import Path

from tools.dependency_review import license_exceptions as check

APPROVED = "pkg:nuget/Microsoft.Data.SqlClient.SNI.runtime@6.0.3"


def added(name, version, purl=None, ecosystem="nuget"):
    return {"change_type": "added", "name": name, "version": version, "ecosystem": ecosystem,
            "package_url": purl if purl is not None else f"pkg:nuget/{name}@{version}"}


class ParseExceptionsTests(unittest.TestCase):
    def test_entries_are_keyed_by_lower_case_name(self):
        # Arrange
        text = f"{APPROVED}, pkg:nuget/Microsoft.Identity.Client.NativeInterop@0.20.6"

        # Act
        approved = check.parse_exceptions(text)

        # Assert
        self.assertEqual({"microsoft.data.sqlclient.sni.runtime": "6.0.3",
                          "microsoft.identity.client.nativeinterop": "0.20.6"}, approved)

    def test_entry_without_version_fails(self):
        # Arrange
        text = "pkg:nuget/Microsoft.Data.SqlClient.SNI.runtime"

        # Act
        with self.assertRaises(check.LicenseExceptionError) as raised:
            check.parse_exceptions(text)

        # Assert
        self.assertIn("pkg:nuget/Name@version", str(raised.exception))


class CheckChangesTests(unittest.TestCase):
    def setUp(self):
        self.approved = check.parse_exceptions(APPROVED)

    def test_exact_reviewed_version_passes(self):
        # Arrange
        changes = [added("Microsoft.Data.SqlClient.SNI.runtime", "6.0.3")]

        # Act
        result = check.check_changes(changes, self.approved)

        # Assert
        self.assertIsNone(result)

    def test_other_version_fails(self):
        # Arrange
        changes = [added("Microsoft.Data.SqlClient.SNI.runtime", "6.0.4")]

        # Act
        with self.assertRaises(check.LicenseExceptionError) as raised:
            check.check_changes(changes, self.approved)

        # Assert
        self.assertIn("6.0.3", str(raised.exception))

    def test_name_in_other_case_with_other_version_fails(self):
        # Arrange
        changes = [added("microsoft.data.sqlclient.sni.runtime", "7.0.0")]

        # Act
        with self.assertRaises(check.LicenseExceptionError) as raised:
            check.check_changes(changes, self.approved)

        # Assert
        self.assertIn("microsoft.data.sqlclient.sni.runtime 6.0.3", str(raised.exception))

    def test_mismatching_package_url_fails(self):
        # Arrange
        changes = [added("Microsoft.Data.SqlClient.SNI.runtime", "6.0.3",
                         purl="pkg:nuget/Microsoft.Data.SqlClient.SNI.runtime@6.0.4")]

        # Act
        with self.assertRaises(check.LicenseExceptionError) as raised:
            check.check_changes(changes, self.approved)

        # Assert
        self.assertIn("microsoft.data.sqlclient.sni.runtime 6.0.3", str(raised.exception))

    def test_package_matched_only_through_package_url_fails(self):
        # Arrange
        changes = [added("Other", "6.0.3", purl="pkg:nuget/Microsoft.Data.SqlClient.SNI.runtime@6.0.3")]

        # Act
        with self.assertRaises(check.LicenseExceptionError) as raised:
            check.check_changes(changes, self.approved)

        # Assert
        self.assertIn("microsoft.data.sqlclient.sni.runtime 6.0.3", str(raised.exception))

    def test_removed_and_unrelated_packages_are_ignored(self):
        # Arrange
        removed = added("Microsoft.Data.SqlClient.SNI.runtime", "5.0.0")
        removed["change_type"] = "removed"
        changes = [removed, added("Serilog", "4.0.0")]

        # Act
        result = check.check_changes(changes, self.approved)

        # Assert
        self.assertIsNone(result)

    def test_unknown_change_type_fails(self):
        # Arrange
        changes = [{"change_type": "updated"}]

        # Act
        with self.assertRaises(check.LicenseExceptionError) as raised:
            check.check_changes(changes, self.approved)

        # Assert
        self.assertIn("Invalid", str(raised.exception))

    def test_non_array_comparison_fails(self):
        # Arrange
        changes = {"change_type": "added"}

        # Act
        with self.assertRaises(check.LicenseExceptionError) as raised:
            check.check_changes(changes, self.approved)

        # Assert
        self.assertIn("array", str(raised.exception))


class ActionNameTests(unittest.TestCase):
    def test_namespaced_package_url_keeps_namespace(self):
        # Arrange
        purl = "pkg:nuget/Microsoft/Data.SqlClient@1.0.0"

        # Act
        identity = check.action_name(purl)

        # Assert
        self.assertEqual("microsoft/data.sqlclient", identity)

    def test_other_ecosystem_is_ignored(self):
        # Arrange
        purl = "pkg:npm/left-pad@1.3.0"

        # Act
        identity = check.action_name(purl)

        # Assert
        self.assertIsNone(identity)


class MainTests(unittest.TestCase):
    def test_json_string_transport_is_decoded(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changes.json"
            path.write_text(json.dumps(json.dumps([added("Microsoft.Data.SqlClient.SNI.runtime", "6.0.3")])),
                            encoding="utf-8")

            # Act
            code = check.main(["--changes", str(path), "--exceptions", APPROVED])

        # Assert
        self.assertEqual(0, code)

    def test_other_version_exits_with_failure(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changes.json"
            path.write_text(json.dumps(json.dumps([added("Microsoft.Data.SqlClient.SNI.runtime", "9.9.9")])),
                            encoding="utf-8")

            # Act
            code = check.main(["--changes", str(path), "--exceptions", APPROVED])

        # Assert
        self.assertEqual(1, code)


if __name__ == "__main__":
    unittest.main()
