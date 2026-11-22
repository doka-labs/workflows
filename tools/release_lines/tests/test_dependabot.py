import json
import unittest
from pathlib import Path

from tools.release_lines import dependabot

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "release-policy.json"
TEMPLATE = """version: 2
updates:
  - package-ecosystem: dotnet-sdk
    directory: /
    schedule:
      interval: weekly
  - package-ecosystem: nuget
    directory: /
    schedule:
      interval: weekly
    cooldown:
      default-days: 7
    ignore:
      - dependency-name: MySqlConnector
        update-types:
          - version-update:semver-major
    groups:
      entity-framework:
        patterns:
          - Microsoft.EntityFrameworkCore.*
"""


def policy(coupling="coupled"):
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    value["toolchain"]["coupling"] = coupling
    return value


class RenderTests(unittest.TestCase):
    def test_each_active_line_gets_every_entry_with_its_branch(self):
        # Arrange
        value = policy()

        # Act
        rendered = dependabot.render(value, TEMPLATE)

        # Assert
        self.assertEqual(["main", "main", "maintenance/10.x", "maintenance/10.x"],
                         [line.split(": ", 1)[1] for line in rendered.splitlines() if "target-branch" in line])

    def test_coupled_line_caps_framework_and_ceiling_packages_below_next_major(self):
        # Arrange
        value = policy()

        # Act
        rendered = dependabot.render(value, TEMPLATE)

        # Assert
        maintenance = rendered.split("target-branch: maintenance/10.x")[2]
        for name in ("Microsoft.EntityFrameworkCore", "Microsoft.EntityFrameworkCore.*",
                     "System.Security.Cryptography.Pkcs"):
            self.assertIn(f'- dependency-name: "{name}"\n        versions: [">=11.0.0-0", ">=11.0.0--"]', maintenance)

    def test_template_rules_and_cooldown_are_kept(self):
        # Arrange
        value = policy()

        # Act
        rendered = dependabot.render(value, TEMPLATE)

        # Assert
        nuget = rendered.split("  - package-ecosystem: nuget\n")[1]
        self.assertIn("    cooldown:\n      default-days: 7\n", nuget)
        self.assertLess(nuget.index("dependency-name: MySqlConnector"),
                        nuget.index('dependency-name: "Microsoft.EntityFrameworkCore"'))
        self.assertLess(nuget.index('dependency-name: "System.Security.Cryptography.Pkcs"'), nuget.index("    groups:"))

    def test_sdk_gets_no_major_updates(self):
        # Arrange
        value = policy()

        # Act
        rendered = dependabot.render(value, TEMPLATE)

        # Assert
        sdk = rendered.split("  - package-ecosystem: dotnet-sdk\n")[1].split("  - package-ecosystem:")[0]
        self.assertIn('      - dependency-name: "*"\n        update-types:\n          - version-update:semver-major\n', sdk)

    def test_independent_toolchain_blocks_framework_majors(self):
        # Arrange
        value = policy("independent")

        # Act
        rendered = dependabot.render(value, TEMPLATE)

        # Assert
        self.assertIn('- dependency-name: "Microsoft.EntityFrameworkCore"\n        update-types:\n'
                      "          - version-update:semver-major\n", rendered)
        self.assertNotIn(">=11.0.0-0", rendered)

    def test_no_active_line_renders_no_updates(self):
        # Arrange
        value = policy()
        for line in value["lines"]:
            line["state"] = "inactive"

        # Act
        rendered = dependabot.render(value, TEMPLATE)

        # Assert
        self.assertTrue(rendered.endswith("version: 2\nupdates: []\n"))

    def test_template_with_target_branch_fails(self):
        # Arrange
        template = TEMPLATE.replace("    directory: /\n", "    directory: /\n    target-branch: main\n", 1)

        # Act
        with self.assertRaises(dependabot.DependabotError) as raised:
            dependabot.render(policy(), template)

        # Assert
        self.assertIn("must not set target-branch", str(raised.exception))

    def test_template_rule_for_framework_package_fails(self):
        # Arrange
        template = TEMPLATE.replace("dependency-name: MySqlConnector", "dependency-name: Microsoft.EntityFrameworkCore")

        # Act
        with self.assertRaises(dependabot.DependabotError) as raised:
            dependabot.render(policy(), template)

        # Assert
        self.assertIn("belong to the generator", str(raised.exception))

    def test_sdk_template_rule_fails(self):
        # Arrange
        template = TEMPLATE.replace("  - package-ecosystem: dotnet-sdk\n    directory: /\n",
                                    "  - package-ecosystem: dotnet-sdk\n    directory: /\n    ignore:\n"
                                    "      - dependency-name: dotnet-sdk\n", 1)

        # Act
        with self.assertRaises(dependabot.DependabotError) as raised:
            dependabot.render(policy(), template)

        # Assert
        self.assertIn("dotnet-sdk template entry", str(raised.exception))

    def test_repeated_key_fails(self):
        # Arrange
        template = TEMPLATE.replace("    schedule:\n      interval: weekly\n  - package-ecosystem: nuget",
                                    "    schedule:\n      interval: weekly\n    schedule:\n      interval: daily\n"
                                    "  - package-ecosystem: nuget", 1)

        # Act
        with self.assertRaises(dependabot.DependabotError) as raised:
            dependabot.render(policy(), template)

        # Assert
        self.assertIn("repeats a key", str(raised.exception))


class CheckTests(unittest.TestCase):
    def test_rendered_configuration_passes(self):
        # Arrange
        committed = dependabot.render(policy(), TEMPLATE)

        # Act
        result = dependabot.check(policy(), TEMPLATE, committed)

        # Assert
        self.assertIsNone(result)

    def test_hand_edited_configuration_fails(self):
        # Arrange
        committed = dependabot.render(policy(), TEMPLATE).replace(">=11.0.0-0", ">=12.0.0-0")

        # Act
        with self.assertRaises(dependabot.DependabotError) as raised:
            dependabot.check(policy(), TEMPLATE, committed)

        # Assert
        self.assertIn("differs", str(raised.exception))


class FlowStyleIgnoreTests(unittest.TestCase):
    def test_flow_style_ignore_fails(self):
        # Arrange
        template = TEMPLATE.replace("    ignore:\n      - dependency-name: MySqlConnector\n        update-types:\n"
                                    "          - version-update:semver-major\n", "    ignore: []\n")

        # Act
        with self.assertRaises(dependabot.DependabotError) as raised:
            dependabot.render(policy(), template)

        # Assert
        self.assertIn("must write ignore as a block list", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
