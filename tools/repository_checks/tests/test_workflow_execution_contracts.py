import re
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
STEP_BLOCK = re.compile(r"^(?P<indent> {2}| {4})steps:[ \t]*\n"
                        r"(?P<body>(?:(?:^(?P=indent)  .*|^[ \t]*)\n)*)", re.MULTILINE)


def steps(path):
    workflow = path.read_text(encoding="utf-8")
    return [step for block in STEP_BLOCK.finditer(workflow)
            for step in re.split(rf"^{block['indent']}  - ", block["body"], flags=re.MULTILINE)[1:]]


def command_step(parts, command):
    return next(part for part in parts if command in part)


class WorkflowExecutionContractTests(unittest.TestCase):
    def test_first_step_can_be_unnamed(self):
        # Arrange
        workflow = ("on:\n  push:\n    tags:\n      - 'v*'\n"
                    "jobs:\n  release:\n    steps:\n      - uses: actions/checkout@v7\n"
                    "      - name: Publish\n        run: publish\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.yml"
            path.write_text(workflow, encoding="ascii")

            # Act
            first, second = steps(path)

        # Assert
        self.assertIn("uses: actions/checkout@v7", first)
        self.assertIn("name: Publish", second)

    def test_base_fetches_configure_gh_in_the_step_that_has_the_token(self):
        # Arrange
        workflows = ("change-scope.yml", "release-pr-attestation.yml")
        for workflow in workflows:
            with self.subTest(workflow=workflow):
                # Act
                fetch = command_step(steps(WORKFLOWS / workflow), "git fetch --no-tags --depth=1")

                # Assert
                self.assertIn("GH_TOKEN: ${{ github.token }}", fetch)
                self.assertIn("gh auth setup-git --hostname github.com", fetch)
                self.assertLess(fetch.index("gh auth setup-git"), fetch.index("git fetch"))

    def test_release_git_reads_configure_the_helper_in_their_job(self):
        # Arrange
        cases = (("release-candidate.yml", "tag-readiness --root"), ("release.yml", "self-release --root"))
        for workflow, command in cases:
            with self.subTest(workflow=workflow):
                # Act
                git_read = command_step(steps(WORKFLOWS / workflow), command)

                # Assert
                self.assertIn("GH_TOKEN: ${{ github.token }}", git_read)
                self.assertIn("gh auth setup-git --hostname github.com", git_read)
                self.assertLess(git_read.index("gh auth setup-git"), git_read.index(command))

    def test_dotnet_executes_from_the_product_checkout_even_for_a_shared_project(self):
        # Arrange
        workflows = ("release-candidate.yml", "package-check.yml")
        action = ROOT / ".github" / "actions" / "release-publish" / "action.yml"

        for workflow in workflows:
            with self.subTest(workflow=workflow):
                # Act
                symbols = command_step(steps(WORKFLOWS / workflow), "dotnet run --project")

                # Assert
                self.assertIn("working-directory: repository", symbols)
                self.assertIn('"$GITHUB_WORKSPACE/.workflows/tools/NuGetSymbols/NuGetSymbols.csproj"', symbols)

        # Act
        push = command_step(steps(action), "dotnet nuget push")

        # Assert
        self.assertIn("working-directory: repository", push)

    def test_publication_requests_a_key_and_pushes_only_what_is_missing(self):
        # Arrange
        action = ROOT / ".github" / "actions" / "release-publish" / "action.yml"
        parts = steps(action)

        # Act
        login = command_step(parts, "NuGet/login@")
        push = command_step(parts, "dotnet nuget push")
        pending = command_step(parts, "pending-packages")

        # Assert
        self.assertIn("id: pending", pending)
        for step in (login, push):
            self.assertIn("if: steps.pending.outputs.push == 'true'", step)
        for loop in push.split("done < ")[:2]:
            self.assertLess(loop.rindex("recheck"), loop.index("dotnet nuget push"))
        symbol_loop = push.split("done < ")[1]
        self.assertLess(symbol_loop.index("symbol-pending"), symbol_loop.index("dotnet nuget push"))

    def test_stage_and_sbom_jobs_read_the_attested_candidate_of_its_attempt(self):
        # Arrange
        workflow = (WORKFLOWS / "release-candidate.yml").read_text(encoding="utf-8")

        # Act
        jobs = dict(re.findall(r"^  ([a-z-]+):\n((?:^(?:    .*|[ \t]*)\n)*)", workflow, flags=re.MULTILINE))
        candidate, stage, sbom, tag_check = jobs["candidate"], jobs["stage"], jobs["sbom"], jobs["tag-check"]

        # Assert
        self.assertNotIn("contents: write", candidate)
        self.assertNotIn("tools.release_publication stage", candidate)
        self.assertIn('echo "name=release-candidate-$VERSION-attempt-$ATTEMPT" >> "$GITHUB_OUTPUT"', candidate)
        self.assertIn("artifact: ${{ steps.artifact.outputs.name }}", candidate)
        for job in (stage, sbom):
            self.assertIn("name: ${{ needs.candidate.outputs.artifact }}", job)
        self.assertIn("contents: write", stage)
        self.assertIn("group: ${{ github.repository }}-candidate-${{ inputs.version }}", stage)
        self.assertLess(stage.index("assemble-candidate --handoff"), stage.index("tools.release_publication stage"))
        self.assertIn("needs: [candidate, stage, sbom]", tag_check)

    def test_unsupported_runners_fail_before_checkout(self):
        # Arrange
        action = ROOT / ".github" / "actions" / "release-publish" / "action.yml"
        linux_x64 = ("runner.os != 'Linux'", "runner.arch != 'X64'")
        hosted_linux = ("runner.environment != 'github-hosted'", "runner.os != 'Linux'")
        cases = (
            (WORKFLOWS / "api-reference.yml", linux_x64),
            (WORKFLOWS / "package-check.yml", linux_x64),
            (WORKFLOWS / "release-candidate.yml", (*hosted_linux, "runner.arch != 'X64'")),
            (WORKFLOWS / "release-pr-attestation.yml", hosted_linux),
            (WORKFLOWS / "release-qualification.yml", hosted_linux),
            (WORKFLOWS / "architecture-document.yml", ("runner.os != 'Linux'",)),
            (WORKFLOWS / "workflow-lint.yml", linux_x64),
            (action, ("runner.environment != 'github-hosted'",)),
        )
        for path, conditions in cases:
            with self.subTest(workflow=path):
                # Act
                first_step = steps(path)[0]

                # Assert
                self.assertNotIn("uses: actions/checkout@", first_step)
                for condition in conditions:
                    self.assertIn(condition, first_step)
                self.assertIn("exit 1", first_step)
