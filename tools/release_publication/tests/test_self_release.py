import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tools.release_lines.github import GitHubError
from tools.release_publication import __main__ as cli
from tools.release_publication import publication, self_release
from tools.release_publication.tests.test_publication import FakeApi

ROOT = Path(__file__).resolve().parents[3]
REPOSITORY = "doka-labs/workflows"
COMMIT = "a" * 40
TAG = "v0.1.0"
NOTES = "# 0.1.0\n\n- Added it.\n"
CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-11-23\n\n- Added it.\n"


def self_check_run(status="completed", conclusion="success", **overrides):
    return {"head_sha": COMMIT, "head_branch": "main", "path": ".github/workflows/self-check.yml",
            "status": status, "conclusion": conclusion, **overrides}


class ReleaseApi(FakeApi):
    """Answers with the routes and records writes; a created release becomes immutable after `delay` reads."""

    def __init__(self, routes, *, runs=None, existing=None, immutable=True, delay=0):
        super().__init__(routes)
        self.runs = list(runs if runs is not None else [[self_check_run()]])
        self.release = existing
        self.immutable = immutable
        self.delay = delay
        self.posts = []

    def request(self, route, method="GET", payload=None):
        if method == "POST":
            self.posts.append((route, payload))
            self.release = {"id": 1, "tag_name": payload["tag_name"], "name": payload["name"], "body": payload["body"],
                            "draft": payload["draft"], "prerelease": payload["prerelease"], "immutable": False,
                            "html_url": "https://github.com/doka-labs/workflows/releases/tag/v0.1.0"}
            return self.release

        if route.startswith(f"repos/{REPOSITORY}/actions/workflows/self-check.yml/runs"):
            return {"workflow_runs": self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]}

        if route.startswith(f"repos/{REPOSITORY}/releases?"):
            if self.release and self.posts:
                if self.delay:
                    self.delay -= 1
                else:
                    self.release["immutable"] = self.immutable

            return [self.release] if self.release else []

        if route == f"repos/{REPOSITORY}/releases/latest" and self.posts:
            return {"tag_name": self.release["tag_name"]}

        return super().request(route, method, payload)


def tag_routes(verified=True, kind="tag"):
    return {f"repos/{REPOSITORY}/git/ref/tags/{TAG}": {"object": {"type": kind, "sha": "d" * 40}},
            f"repos/{REPOSITORY}/git/tags/{'d' * 40}": {
                "object": {"type": "commit", "sha": COMMIT},
                "verification": {"verified": verified, "reason": "valid" if verified else "unsigned"}}}


class SelfReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        self.original = publication.run
        self.commands = []
        publication.run = lambda command, **kwargs: self.commands.append(command) or ""
        self.sleeps = []

    def tearDown(self):
        publication.run = self.original
        self.directory.cleanup()

    def release(self, api, *, tag=TAG, checkout=COMMIT, belongs=True, attempts=3):
        return self_release.release(api, self.root, REPOSITORY, tag, attempts=attempts, sleep=self.sleeps.append,
                                    checkout=lambda root: checkout, belongs=lambda root, commit: belongs)

    def test_signed_tag_on_main_with_green_self_check_is_released(self):
        # Arrange
        api = ReleaseApi(tag_routes())

        # Act
        result = self.release(api)

        # Assert
        self.assertEqual([(f"repos/{REPOSITORY}/releases", {
            "tag_name": TAG, "name": TAG, "body": NOTES, "draft": False, "prerelease": False,
            "make_latest": "true"})], api.posts)
        self.assertEqual(["gh", "release", "verify", TAG, "--repo", REPOSITORY], self.commands[-1])
        self.assertEqual(COMMIT, result["commit"])

    def test_prerelease_tag_is_released_as_prerelease_and_not_latest(self):
        # Arrange
        (self.root / "CHANGELOG.md").write_text("## [0.2.0-rc.1] - 2026-11-24\n\n- Added it.\n", encoding="utf-8")
        routes = {key.replace(TAG, "v0.2.0-rc.1"): value for key, value in tag_routes().items()}
        api = ReleaseApi(routes)

        # Act
        self.release(api, tag="v0.2.0-rc.1")

        # Assert
        self.assertEqual((True, "false"), (api.posts[0][1]["prerelease"], api.posts[0][1]["make_latest"]))

    def test_tag_without_version_fails(self):
        # Arrange
        api = ReleaseApi(tag_routes())

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api, tag="v0.1")

        # Assert
        self.assertIn("canonical version", str(raised.exception))
        self.assertEqual([], api.posts)

    def test_unsigned_tag_fails(self):
        # Arrange
        api = ReleaseApi(tag_routes(verified=False))

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api)

        # Assert
        self.assertIn("does not verify", str(raised.exception))
        self.assertEqual([], api.posts)

    def test_lightweight_tag_fails(self):
        # Arrange
        api = ReleaseApi(tag_routes(kind="commit"))

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api)

        # Assert
        self.assertIn("not an annotated tag", str(raised.exception))

    def test_other_checkout_fails(self):
        # Arrange
        api = ReleaseApi(tag_routes())

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api, checkout="b" * 40)

        # Assert
        self.assertIn("checkout is not the commit", str(raised.exception))

    def test_commit_outside_main_fails(self):
        # Arrange
        api = ReleaseApi(tag_routes())

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api, belongs=False)

        # Assert
        self.assertIn("not part of main", str(raised.exception))
        self.assertEqual([], api.posts)

    def test_failed_self_check_fails(self):
        # Arrange
        api = ReleaseApi(tag_routes(), runs=[[self_check_run(conclusion="failure")]])

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api)

        # Assert
        self.assertIn("Self Check failed", str(raised.exception))
        self.assertEqual([], api.posts)

    def test_running_self_check_is_awaited(self):
        # Arrange
        api = ReleaseApi(tag_routes(), runs=[[], [self_check_run(status="in_progress", conclusion=None)],
                                             [self_check_run()]])

        # Act
        self.release(api)

        # Assert
        self.assertEqual([20, 20], self.sleeps[:2])
        self.assertEqual(1, len(api.posts))

    def test_self_check_of_another_branch_or_workflow_does_not_count(self):
        # Arrange
        runs = [self_check_run(head_branch="feature"), self_check_run(path=".github/workflows/other.yml")]
        api = ReleaseApi(tag_routes(), runs=[[*runs, self_check_run(conclusion="failure")]])

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api)

        # Assert
        self.assertIn("Self Check failed", str(raised.exception))

    def test_missing_changelog_section_fails(self):
        # Arrange
        (self.root / "CHANGELOG.md").write_text("## [Unreleased]\n\n- Added it.\n", encoding="utf-8")
        api = ReleaseApi(tag_routes())

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api)

        # Assert
        self.assertIn("exactly one dated section", str(raised.exception))
        self.assertEqual([], api.posts)

    def test_rerun_verifies_its_published_release_without_writing(self):
        # Arrange
        existing = {"id": 1, "tag_name": TAG, "name": TAG, "body": NOTES, "draft": False, "prerelease": False,
                    "immutable": True, "html_url": "https://example.invalid/v0.1.0"}
        api = ReleaseApi(tag_routes(), existing=existing)

        # Act
        result = self.release(api)

        # Assert
        self.assertEqual([], api.posts)
        self.assertEqual(["gh", "release", "verify", TAG, "--repo", REPOSITORY], self.commands[-1])
        self.assertEqual("https://example.invalid/v0.1.0", result["url"])

    def test_other_release_of_the_tag_fails(self):
        # Arrange
        existing = {"id": 1, "tag_name": TAG, "name": TAG, "body": "other", "draft": False, "prerelease": False,
                    "immutable": True}
        api = ReleaseApi(tag_routes(), existing=existing)

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api)

        # Assert
        self.assertIn("another state, title, notes", str(raised.exception))
        self.assertEqual([], api.posts)

    def test_draft_of_the_tag_fails(self):
        # Arrange
        existing = {"id": 1, "tag_name": TAG, "name": TAG, "body": NOTES, "draft": True, "prerelease": False}
        api = ReleaseApi(tag_routes(), existing=existing)

        # Act
        with self.assertRaises(publication.PublicationError):
            self.release(api)

        # Assert
        self.assertEqual([], api.posts)

    def test_release_that_becomes_immutable_later_is_awaited(self):
        # Arrange
        api = ReleaseApi(tag_routes(), delay=2)

        # Act
        self.release(api, attempts=5)

        # Assert
        self.assertEqual([10, 10], self.sleeps)

    def test_release_that_never_becomes_immutable_fails(self):
        # Arrange
        api = ReleaseApi(tag_routes(), immutable=False)

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api)

        # Assert
        self.assertIn("did not become an immutable", str(raised.exception))

    def test_failed_attestation_check_is_retried_then_fails(self):
        # Arrange
        api = ReleaseApi(tag_routes())

        def fail(command, **kwargs):
            raise publication.PublicationError("Command failed: gh release verify")

        publication.run = fail

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.release(api)

        # Assert
        self.assertIn("did not become an immutable", str(raised.exception))
        self.assertEqual([10, 10], self.sleeps)

    def test_missing_tag_fails(self):
        # Arrange
        api = ReleaseApi({})

        # Act
        with self.assertRaises(GitHubError):
            self.release(api)

        # Assert
        self.assertEqual([], api.posts)

    def test_lower_version_after_a_higher_latest_is_not_made_latest(self):
        # Arrange
        routes = {**tag_routes(), f"repos/{REPOSITORY}/releases/latest": {
            "tag_name": "v0.2.0", "immutable": True, "draft": False, "prerelease": False},
            f"repos/{REPOSITORY}/git/ref/tags/v0.2.0": {"object": {"type": "tag", "sha": "e" * 40}},
            f"repos/{REPOSITORY}/git/tags/{'e' * 40}": {"object": {"type": "commit", "sha": "f" * 40},
                                                        "verification": {"verified": True, "reason": "valid"}}}
        api = ReleaseApi(routes)

        # Act
        self.release(api)

        # Assert
        self.assertEqual("false", api.posts[0][1]["make_latest"])


class SelfReleaseCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.summary = self.root / "summary.md"
        self.output = self.root / "output.txt"

    def tearDown(self):
        self.directory.cleanup()

    def run_command(self, release):
        # WHY: On GitHub the job's own GITHUB_STEP_SUMMARY and GITHUB_OUTPUT are set; the command must write into
        # files of this test, or the Tool Tests summary shows a release that never happened.
        environment = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_STEP_SUMMARY": str(self.summary),
                       "GITHUB_OUTPUT": str(self.output)}
        with unittest.mock.patch.dict(os.environ, environment, clear=True), \
                unittest.mock.patch.object(self_release, "release", release):
            return cli.main(["self-release", "--root", str(self.root), "--tag", TAG])

    def test_command_releases_the_tag(self):
        # Arrange
        calls = []

        def release(api, root, repository, tag):
            calls.append((root, repository, tag))
            return {"tag": tag, "commit": COMMIT, "url": "https://example.invalid"}

        # Act
        code = self.run_command(release)

        # Assert
        self.assertEqual(0, code)
        self.assertEqual([(self.root, REPOSITORY, TAG)], calls)
        self.assertEqual("## Released v0.1.0\n\nhttps://example.invalid\n", self.summary.read_text(encoding="utf-8"))
        self.assertFalse(self.output.exists())

    def test_command_reports_a_failed_release(self):
        # Arrange
        def release(api, root, repository, tag):
            raise publication.PublicationError("Self Check failed.")

        # Act
        with unittest.mock.patch("sys.stderr"):
            code = self.run_command(release)

        # Assert
        self.assertEqual(1, code)
        self.assertFalse(self.summary.exists())


class ReleaseWorkflowTests(unittest.TestCase):
    def test_releases_of_all_tags_run_one_after_another(self):
        # Arrange
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        # Act
        concurrency = workflow.split("concurrency:\n", 1)[1].split("\n\n", 1)[0]

        # Assert
        self.assertEqual("  group: ${{ github.workflow }}\n  cancel-in-progress: false\n  queue: max", concurrency)


if __name__ == "__main__":
    unittest.main()
