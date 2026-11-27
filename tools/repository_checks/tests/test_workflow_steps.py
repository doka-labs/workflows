import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def run_block(file: str, step: str) -> str:
    """Get the shell block of the named final step in one workflow."""
    workflow = (WORKFLOWS / file).read_text(encoding="utf-8")
    return textwrap.dedent(workflow.split(f"- name: {step}\n", 1)[1].split("        run: |\n", 1)[1])


@unittest.skipUnless(shutil.which("bash"), "Bash is required by the tested steps.")
class WorkflowStepsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "repository"
        self.checkout.mkdir()
        self.calls = self.root / "calls"
        self.env = {**os.environ, "PATH": f"{self.root}{os.pathsep}{os.environ['PATH']}", "CALLS": str(self.calls)}

    def stub(self, command: str, script: str) -> None:
        path = self.root / command
        path.write_text(f"#!/bin/bash\n{script}", encoding="utf-8")
        path.chmod(0o755)

    def run_step(self, workflow: str, step: str, *, cwd: Path | None = None,
                 **environment: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-e", "-c", run_block(workflow, step)], cwd=cwd or self.checkout,
                              env={**self.env, **environment}, capture_output=True, text=True, timeout=15)

    def test_assignment_distinguishes_ineligible_author_from_failed_request(self):
        # Arrange
        self.stub("gh", textwrap.dedent("""\
            if [[ "$*" == *'-X POST'* ]]; then
              echo POST > "$CALLS"
              exit 0
            fi
            case "$RESPONSE" in
              204) printf 'HTTP/2.0 204 No Content\\n';;
              404|503) printf 'HTTP/2.0 %s\\n' "$RESPONSE"; exit 1;;
              transport) exit 1;;
            esac
            """))

        for status, expected, should_assign in (("204", 0, True), ("404", 0, False), ("503", 1, False),
                                                ("transport", 1, False)):
            with self.subTest(status=status):
                self.calls.unlink(missing_ok=True)

                # Act
                result = self.run_step("pr-assign.yml", "Assign the author", RESPONSE=status, AUTHOR="example",
                                       NUMBER="7", GITHUB_REPOSITORY="owner/repo")

                # Assert
                self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
                self.assertEqual(should_assign, self.calls.exists())
