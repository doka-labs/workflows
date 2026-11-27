"""Validate the exact candidate archives with the SDK's package validator, outside the pack that produced them."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from tools.release_publication import nuget

TOOL = "Microsoft.DotNet.ApiCompat.Tool"
# WHY: The properties and items that Microsoft.NET.ApiCompat.ValidatePackage.targets passes to ValidatePackageTask,
# besides the package, the baseline and the references, which are handled separately. Empty values keep the task's
# defaults, which the tool shares (RunApiCompat and EnableStrictModeForCompatibleTfms default to true in both).
# https://github.com/dotnet/sdk/blob/v10.0.404/src/Tasks/Microsoft.NET.Build.Tasks/targets/Microsoft.NET.ApiCompat.ValidatePackage.targets#L39-L59
# https://github.com/dotnet/sdk/blob/v10.0.404/src/Compatibility/ApiCompat/Microsoft.DotNet.ApiCompat.Task/ValidatePackageTask.cs#L59-L64
# https://github.com/dotnet/sdk/blob/v10.0.404/src/Compatibility/ApiCompat/Microsoft.DotNet.ApiCompat.Tool/Program.cs#L236-L245
BOOLEANS = {
    "ApiCompatRespectInternals": "--respect-internals",
    "ApiCompatEnableRuleAttributesMustMatch": "--enable-rule-attributes-must-match",
    "ApiCompatEnableRuleCannotChangeParameterName": "--enable-rule-cannot-change-parameter-name",
    "RunApiCompat": "--run-api-compat",
    "EnableStrictModeForCompatibleTfms": "--enable-strict-mode-for-compatible-tfms",
    "EnableStrictModeForCompatibleFrameworksInPackage": "--enable-strict-mode-for-compatible-frameworks-in-package",
    "EnableStrictModeForBaselineValidation": "--enable-strict-mode-for-baseline-validation",
    "ApiCompatPreserveUnnecessarySuppressions": "--preserve-unnecessary-suppressions",
    "ApiCompatPermitUnnecessarySuppressions": "--permit-unnecessary-suppressions",
}
VALUES = {"NoWarn": "--noWarn", "RoslynAssembliesPath": "--roslyn-assemblies-path",
          "RuntimeIdentifierGraphPath": "--runtime-graph"}
ITEMS = {"ApiCompatSuppressionFile": "--suppression-file",
         "ApiCompatExcludeAttributesFile": "--exclude-attributes-file",
         "PackageValidationBaselineFrameworkToIgnore": "--baseline-package-frameworks-to-ignore"}
PROPERTIES = ["PackageId", "IsPackable", "EnablePackageValidation", "TargetFramework", "TargetFrameworks",
              "PackageValidationBaselineName", "PackageValidationBaselineVersion", "PackageValidationBaselinePath",
              "DisablePackageBaselineValidation", "RunPackageValidationWithoutReferences",
              "ApiCompatGenerateSuppressionFile", *BOOLEANS, *VALUES]


class PackageValidationError(RuntimeError):
    """A candidate archive cannot be validated as the pack would have validated it."""


def property_arguments(settings: dict[str, str]) -> list[str]:
    """Pass property values without MSBuild reading list delimiters in them as further assignments."""
    # WHY: https://learn.microsoft.com/visualstudio/msbuild/special-characters-to-escape
    return [f"-property:{name}=" + value.replace("%", "%25").replace(";", "%3B").replace(",", "%2C")
            for name, value in settings.items()]


def msbuild(root: Path, project: Path, arguments: list[str], *, run=subprocess.run) -> dict:
    """Run MSBuild in the checkout, whose global.json selects the product's SDK, and return its JSON result."""
    # WHY: https://learn.microsoft.com/dotnet/core/tools/global-json
    completed = run(["dotnet", "msbuild", str(project), "-nologo", *arguments], cwd=root, capture_output=True,
                    text=True, timeout=600, check=False)
    try:
        result = json.loads(completed.stdout)
    except (TypeError, ValueError):
        result = None

    if completed.returncode or not isinstance(result, dict):
        raise PackageValidationError(f"MSBuild cannot evaluate {project.relative_to(root).as_posix()}.")

    return result


def collect(root: Path, project: Path, settings: dict[str, str], *, run=subprocess.run) -> dict:
    """Return the validation settings of a project after the SDK filled in its defaults."""
    # WHY: CollectApiCompatInputs adds the default CompatibilitySuppressions.xml next to the project, the SDK's
    # Roslyn path and the legacy property names before the SDK validates a package.
    # https://github.com/dotnet/sdk/blob/v10.0.404/src/Tasks/Microsoft.NET.Build.Tasks/targets/Microsoft.NET.ApiCompat.Common.targets
    result = msbuild(root, project, ["-target:CollectApiCompatInputs", f"-getProperty:{','.join(PROPERTIES)}",
                                     f"-getItem:{','.join(ITEMS)}", *property_arguments(settings)], run=run)
    properties, items = result.get("Properties"), result.get("Items", {})
    if not isinstance(properties, dict) or not isinstance(items, dict):
        raise PackageValidationError(f"MSBuild returned no settings for {project.relative_to(root).as_posix()}.")

    return {"properties": properties,
            "items": {name: [entry["Identity"] for entry in items.get(name, [])] for name in ITEMS}}


def frameworks(state: dict) -> list[str]:
    """Return the target frameworks the pack built, in project order."""
    properties = state["properties"]
    values = properties.get("TargetFrameworks") or properties.get("TargetFramework") or ""
    return list(dict.fromkeys(name.strip() for name in values.split(";") if name.strip()))


def references(root: Path, project: Path, settings: dict[str, str], state: dict, *, run=subprocess.run) -> list[str]:
    """Return the reference arguments the SDK passes, one per target framework, unless the project opts out."""
    if state["properties"].get("RunPackageValidationWithoutReferences", "").lower() == "true":
        return []

    # WHY: The SDK asks each inner build for GetReferencesForApiCompatValidatePackage with BuildProjectReferences
    # false; the item carries the target framework moniker, the platform moniker and the references.
    # https://github.com/dotnet/sdk/blob/v10.0.404/src/Tasks/Microsoft.NET.Build.Tasks/targets/Microsoft.NET.ApiCompat.ValidatePackage.targets#L65-L87
    # https://github.com/dotnet/sdk/blob/v10.0.404/src/Compatibility/ApiCompat/Microsoft.DotNet.ApiCompat.Tool/Program.cs#L395-L438
    arguments = []
    for framework in frameworks(state):
        target = "GetReferencesForApiCompatValidatePackage"
        result = msbuild(root, project, [f"-target:{target}", f"-getTargetResult:{target}", *property_arguments(
            {**settings, "TargetFramework": framework, "BuildProjectReferences": "false"})], run=run)
        try:
            entries = result["TargetResults"][target]["Items"]
        except (KeyError, TypeError) as error:
            raise PackageValidationError(f"MSBuild returned no references of {framework}.") from error

        for entry in entries:
            paths = [path for path in entry.get("ReferencePath", "").split(",") if path]
            missing = [path for path in paths if not Path(path).is_file()]
            if missing:
                raise PackageValidationError(f"A reference of {framework} is missing after the build: {missing[0]}.")

            moniker = entry.get("TargetFrameworkMoniker", "")
            platform = entry.get("TargetPlatformMoniker", "")
            if moniker and paths:
                arguments.append(f"{moniker}{'+' + platform if platform else ''}|{','.join(paths)}")

    return arguments


def fetch_verified(package_id: str, version: str, directory: Path, root: Path, *, fetcher=nuget.fetch,
                   verifier=nuget.verify_signature) -> Path:
    """Download one package from nuget.org and require its signatures to verify with the product's SDK."""
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / "global.json", directory / "global.json")
    status, content = fetcher(nuget.package_url(nuget.package_base(fetcher), {"id": package_id, "version": version}),
                              {}, 120)
    if status != 200 or not content:
        raise PackageValidationError(f"nuget.org did not return {package_id} {version} (HTTP {status}).")

    path = directory / f"{package_id.lower()}.{version.lower()}.nupkg"
    path.write_bytes(content)
    verifier(path, 300)
    return path


def baseline(state: dict, directory: Path, root: Path, *, fetcher=nuget.fetch,
             verifier=nuget.verify_signature) -> Path | None:
    """Return the baseline archive the SDK would compare with, downloaded from nuget.org, or None."""
    properties = state["properties"]
    if properties.get("DisablePackageBaselineValidation", "").lower() == "true":
        return None

    # WHY: The SDK compares with PackageValidationBaselinePath whenever it is set, also without a version, and
    # derives it from the version only when it is empty; DisablePackageBaselineValidation alone switches the
    # baseline off. A fixed path names a local file the candidate cannot prove, so it is refused instead of
    # skipped. The default name is the PackageId.
    # https://github.com/dotnet/sdk/blob/v10.0.404/src/Tasks/Microsoft.NET.Build.Tasks/targets/Microsoft.NET.ApiCompat.ValidatePackage.targets#L27-L31
    if properties.get("PackageValidationBaselinePath"):
        raise PackageValidationError("PackageValidationBaselinePath is not supported; set the baseline version.")

    version = properties.get("PackageValidationBaselineVersion", "")
    if not version:
        return None

    name = properties.get("PackageValidationBaselineName") or properties["PackageId"]
    return fetch_verified(name, version, directory, root, fetcher=fetcher, verifier=verifier)


def tool_arguments(state: dict, package: Path, reference_arguments: list[str], baseline_path: Path | None) -> list[str]:
    """Return the package command with exactly the settings the SDK would pass to ValidatePackageTask."""
    properties, items = state["properties"], state["items"]
    if properties.get("ApiCompatGenerateSuppressionFile", "").lower() == "true":
        raise PackageValidationError("A release cannot generate suppression files; "
                                     "remove ApiCompatGenerateSuppressionFile.")

    arguments = ["package", str(package)]
    for name, option in BOOLEANS.items():
        value = properties.get(name, "").strip().lower()
        if value:
            if value not in ("true", "false"):
                raise PackageValidationError(f"{name} must be true or false, not {properties[name]!r}.")

            arguments.extend([option, value])

    for name, option in VALUES.items():
        if properties.get(name):
            arguments.extend([option, properties[name]])

    for name, option in ITEMS.items():
        for value in items[name]:
            arguments.extend([option, value])

    if baseline_path is not None:
        arguments.extend(["--baseline-package", str(baseline_path)])

    for value in reference_arguments:
        arguments.extend(["--package-assembly-references", value])

    return arguments


def install_tool(root: Path, directory: Path, *, run=subprocess.run, fetcher=nuget.fetch,
                 verifier=nuget.verify_signature) -> Path:
    """Install the tool of the product's SDK version from its verified nuget.org archive and return its path."""
    # WHY: The tool ships with every SDK version as its own package, and its framework is that SDK's runtime.
    # Installing from the verified archive alone keeps the bytes the same as those checked.
    # https://learn.microsoft.com/dotnet/fundamentals/apicompat/global-tool
    # https://learn.microsoft.com/dotnet/core/tools/dotnet-tool-install
    completed = run(["dotnet", "--version"], cwd=root, capture_output=True, text=True, timeout=120, check=False)
    version = completed.stdout.strip()
    if completed.returncode or not version:
        raise PackageValidationError("Cannot read the SDK version of the checkout.")

    source = fetch_verified(TOOL, version, directory / "source", root, fetcher=fetcher, verifier=verifier)
    config = directory / "nuget.config"
    config.write_text('<?xml version="1.0" encoding="utf-8"?>\n<configuration>\n  <packageSources>\n    <clear />\n'
                      f'    <add key="verified" value="{source.parent}" />\n  </packageSources>\n</configuration>\n',
                      encoding="utf-8")
    tools = directory / "tool"
    completed = run(["dotnet", "tool", "install", TOOL, "--version", version, "--tool-path", str(tools),
                     "--configfile", str(config)], cwd=root, capture_output=True, text=True, timeout=600, check=False)
    executable = tools / "apicompat"
    if completed.returncode or not executable.is_file():
        raise PackageValidationError(f"Cannot install {TOOL} {version}.")

    return executable
