import tempfile
import unittest
from pathlib import Path, PurePosixPath

from tools.api_reference import links

REPOSITORY = "doka-labs/Doka.Example"
TAG = "v1.2.0"
BLOB = "https://github.com/doka-labs/Doka.Example/blob/v1.2.0"
TREE = "https://github.com/doka-labs/Doka.Example/tree/v1.2.0"
RECORD = PurePosixPath("docs/decisions/D-001-record.md")


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for path in ("src/Doka.Example/Reader.cs", "LICENSE", "docs/guides/csv.md", "docs/decisions/D-001-record.md",
                     "README.md", "docs/images/flow.png", "assets/logo.png"):
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text("x\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def rewrite(self, text, source=RECORD):
        return links.rewrite(text, source, self.root, REPOSITORY, TAG)

    def test_link_to_a_source_file_goes_to_the_file_at_the_tag(self):
        # Arrange
        text = "- [Reader](../../src/Doka.Example/Reader.cs#L10) reads rows."

        # Act
        result = self.rewrite(text)

        # Assert
        self.assertEqual(f"- [Reader]({BLOB}/src/Doka.Example/Reader.cs#L10) reads rows.", result)

    def test_link_to_a_directory_goes_to_the_tree_at_the_tag(self):
        # Arrange
        text = "See [the sources](../../src/Doka.Example)."

        # Act
        result = self.rewrite(text)

        # Assert
        self.assertEqual(f"See [the sources]({TREE}/src/Doka.Example).", result)

    def test_reference_definition_is_rewritten(self):
        # Arrange
        text = "[license]: ../../LICENSE \"License\""

        # Act
        result = self.rewrite(text)

        # Assert
        self.assertEqual(f"[license]: {BLOB}/LICENSE \"License\"", result)

    def test_links_to_site_documents_and_anchors_stay(self):
        # Arrange
        text = ("[CSV](../guides/csv.md#dialect), [top](#context), [web](https://example.com/a.cs), "
                "![flow](../images/flow.png)")

        # Act
        result = self.rewrite(text)

        # Assert
        self.assertEqual(text, result)

    def test_link_to_the_readme_goes_to_the_index(self):
        # Arrange
        text = "[Overview](../../README.md#install)"

        # Act
        result = self.rewrite(text)

        # Assert
        self.assertEqual("[Overview](../../index.md#install)", result)

    def test_link_with_code_in_its_text_is_rewritten(self):
        # Arrange
        text = "[`Reader`](../../src/Doka.Example/Reader.cs)"

        # Act
        result = self.rewrite(text)

        # Assert
        self.assertEqual(f"[`Reader`]({BLOB}/src/Doka.Example/Reader.cs)", result)

    def test_code_blocks_and_code_spans_stay(self):
        # Arrange
        text = ("Use `[x](../../LICENSE)` literally.\n"
                "```markdown\n[License](../../LICENSE)\n```\n"
                "~~~~\n```\n[License](../../LICENSE)\n~~~~\n"
                "[License](../../LICENSE)")

        # Act
        result = self.rewrite(text)

        # Assert
        self.assertEqual(text.rsplit("\n", 1)[0] + f"\n[License]({BLOB}/LICENSE)", result)

    def test_link_whose_text_runs_over_two_lines_is_rewritten(self):
        # Arrange
        text = "- [Reader in\n  Example](../../src/Doka.Example/Reader.cs): reads rows.\n- [Gone](../../src/Gone.cs)"

        # Act
        result = self.rewrite(text)

        # Assert
        self.assertEqual(f"- [Reader in\n  Example]({BLOB}/src/Doka.Example/Reader.cs): reads rows.\n"
                         "- [Gone](../../src/Gone.cs)", result)

    def test_unclosed_backtick_does_not_hide_the_next_paragraph(self):
        # Arrange
        text = "A stray ` backtick.\n\n[License](../../LICENSE) and `code`."

        # Act
        result = self.rewrite(text)

        # Assert
        self.assertEqual(f"A stray ` backtick.\n\n[License]({BLOB}/LICENSE) and `code`.", result)

    def test_embedded_file_names_its_line(self):
        # Arrange
        text = "Intro.\n\n[!code-csharp[Reader](../../src/Doka.Example/Reader.cs)]"

        # Act
        with self.assertRaises(links.LinkError) as raised:
            self.rewrite(text)

        # Assert
        self.assertIn("D-001-record.md:3:", str(raised.exception))

    def test_link_to_nothing_stays_for_docfx_to_report(self):
        # Arrange
        text = "[Gone](../../src/Gone.cs)"

        # Act
        result = self.rewrite(text)

        # Assert
        self.assertEqual(text, result)

    def test_link_leaving_the_repository_fails(self):
        # Arrange
        text = "[Outside](../../../other/file.cs)"

        # Act
        with self.assertRaises(links.LinkError) as raised:
            self.rewrite(text)

        # Assert
        self.assertIn("docs/decisions/D-001-record.md:1: '../../../other/file.cs' leaves the repository",
                      str(raised.exception))

    def test_image_outside_docs_fails(self):
        # Arrange
        text = "![Logo](../../assets/logo.png)"

        # Act
        with self.assertRaises(links.LinkError) as raised:
            self.rewrite(text)

        # Assert
        self.assertIn("the image '../../assets/logo.png' must lie under docs/", str(raised.exception))

    def test_embedded_file_fails(self):
        # Arrange
        text = "[!code-csharp[Reader](../../src/Doka.Example/Reader.cs)]"

        # Act
        with self.assertRaises(links.LinkError) as raised:
            self.rewrite(text)

        # Assert
        self.assertIn("an embedded file is not supported by the site", str(raised.exception))

    def test_readme_links_resolve_from_the_root(self):
        # Arrange
        text = "[License](LICENSE) and [CSV](docs/guides/csv.md)"

        # Act
        result = self.rewrite(text, PurePosixPath("README.md"))

        # Assert
        self.assertEqual(f"[License]({BLOB}/LICENSE) and [CSV](docs/guides/csv.md)", result)


if __name__ == "__main__":
    unittest.main()
