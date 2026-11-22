import unittest
from pathlib import Path

from tools.repository_checks import artifact_uploads

ROOT = Path(__file__).resolve().parents[3]


def workflow(with_block):
    return ("jobs:\n  a:\n    runs-on: ubuntu-26.04\n    steps:\n"
            "      - name: Upload\n        uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9\n"
            "        with:\n" + with_block +
            "      - name: Next\n        run: \"true\"\n")


class RepositoryTests(unittest.TestCase):
    def test_every_upload_of_this_repository_passes(self):
        # Arrange
        root = ROOT

        # Act
        count = artifact_uploads.check_repository(root)

        # Assert
        self.assertGreaterEqual(count, 2)


class CheckWorkflowTests(unittest.TestCase):
    def check(self, with_block):
        return artifact_uploads.check_workflow(workflow(with_block), "test.yml")

    def failure(self, with_block):
        with self.assertRaises(artifact_uploads.UploadError) as raised:
            self.check(with_block)

        return str(raised.exception)

    def test_named_text_files_pass(self):
        # Arrange
        block = ("          name: evidence\n          path: |\n            out/summary.json\n"
                 "            out/restore.log\n          retention-days: 30\n")

        # Act
        count = self.check(block)

        # Assert
        self.assertEqual(1, count)

    def test_single_path_value_passes(self):
        # Arrange
        block = "          path: ${{ runner.temp }}/release-lines/report.json\n"

        # Act
        count = self.check(block)

        # Assert
        self.assertEqual(1, count)

    def test_directory_fails(self):
        # Arrange
        block = "          path: |\n            out/summary.json\n            artifacts/ef-tooling/mariadb\n"

        # Act
        message = self.failure(block)

        # Assert
        self.assertIn("'artifacts/ef-tooling/mariadb' is not a text evidence file", message)

    def test_pattern_fails(self):
        # Arrange
        block = "          path: '**/TestResults/*.trx'\n"

        # Act
        message = self.failure(block)

        # Assert
        self.assertIn("is a pattern", message)

    def test_binary_file_fails(self):
        # Arrange
        block = "          path: artifacts/ef-tooling/efbundle.exe\n"

        # Act
        message = self.failure(block)

        # Assert
        self.assertIn("not a text evidence file", message)

    def test_uncompressed_upload_fails(self):
        # Arrange
        block = "          path: out/summary.json\n          compression-level: 0\n"

        # Act
        message = self.failure(block)

        # Assert
        self.assertIn("compression-level 0", message)

    def test_path_outside_workspace_fails(self):
        # Arrange
        block = "          path: ../other/summary.json\n"

        # Act
        message = self.failure(block)

        # Assert
        self.assertIn("not a single file inside the workspace", message)

    def test_exclusion_pattern_fails(self):
        # Arrange
        block = "          path: |\n            out/summary.json\n            !out/cache.json\n"

        # Act
        message = self.failure(block)

        # Assert
        self.assertIn("is a pattern", message)

    def test_upload_without_path_fails(self):
        # Arrange
        block = "          name: evidence\n"

        # Act
        message = self.failure(block)

        # Assert
        self.assertIn("no path input", message)


if __name__ == "__main__":
    unittest.main()
