import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LEVELS = {"none": 0, "read": 1, "write": 2}
PERMISSION = re.compile(r"^\s+([a-z-]+): (read|write|none)\s*$", re.MULTILINE)


def permissions(block):
    return dict(PERMISSION.findall(block))


def section(text, indent):
    match = re.search(rf"^{indent}permissions:\n((?:{indent}  .*\n)*)", text, re.MULTILINE)
    return permissions(match[1]) if match else None


def jobs(text, indent):
    return re.finditer(rf"^{indent}([a-z0-9_-]+):\n((?:{indent}  .*\n|\n)*)", text, re.MULTILINE)


def required(workflow):
    text = workflow.read_text(encoding="utf-8")
    top = section(text, "") or {}
    result = {}
    for job in jobs(text.split("\njobs:\n", 1)[1], "  "):
        for name, level in (section(job[2], "    ") or top).items():
            if LEVELS[level] > LEVELS[result.get(name, "none")]:
                result[name] = level

    return result


class ReadmePermissionTests(unittest.TestCase):
    def test_every_caller_example_grants_what_the_called_jobs_request(self):
        # Arrange
        # WHY: "The GITHUB_TOKEN permissions passed from the caller workflow can be only downgraded (not elevated) by
        # the called workflow."
        # https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        gaps = []

        # Act
        for block in re.findall(r"```yaml\n(.*?)```", readme, re.DOTALL):
            top = section(block, "")
            for job in jobs(block, "  "):
                called = re.search(r"uses: [^\s]+/\.github/workflows/([a-z-]+\.yml)@", job[2])
                given = section(job[2], "    ") or top
                if called is None or given is None:
                    continue

                for name, level in required(ROOT / ".github" / "workflows" / called[1]).items():
                    if LEVELS[level] > LEVELS[given.get(name, "none")]:
                        gaps.append(f"{job[1]} -> {called[1]}: {name}: {level}")

        # Assert
        self.assertEqual([], gaps)
