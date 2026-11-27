"""State of candidate packages on nuget.org: availability before publication and readback after it."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from tools.release_publication import packages

INDEX = "https://api.nuget.org/v3/index.json"
# WHY: "Package validation and indexing usually take less than 15 minutes"; one hour leaves room for slow days
# without holding the job for hours. A run that hits the deadline is resumed by re-running the job.
# https://learn.microsoft.com/nuget/nuget-org/publish-a-package
READBACK_SECONDS = 3600
POLL_SECONDS = 10


class NuGetError(RuntimeError):
    """nuget.org holds conflicting bytes or a request failed in a way that proves nothing."""


class PendingError(NuGetError):
    """nuget.org does not yet serve the expected package, signature or symbols."""


def fetch(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    """Read one HTTPS response within the timeout; return status 0 when no answer arrived."""
    if urlsplit(url).scheme != "https":
        raise NuGetError("Only HTTPS endpoints are allowed.")

    deadline = time.monotonic() + timeout
    try:
        with urlopen(Request(url, headers={"User-Agent": "doka-labs-workflows/1", **headers}),
                     timeout=timeout) as response:
            if urlsplit(response.url).scheme != "https":
                raise NuGetError("nuget.org redirected outside HTTPS.")

            content = bytearray()
            # WHY: read() can wait on a slow trickle of bytes despite the socket timeout; NestedSet reads chunks
            # and rechecks the deadline between them.
            while time.monotonic() < deadline:
                block = response.read1(1024 * 1024)
                if not block:
                    return response.status, bytes(content)

                content.extend(block)

            return 0, b""
    except HTTPError as error:
        return error.code, b""
    except (URLError, TimeoutError, ConnectionError, OSError, HTTPException):
        return 0, b""


def require_response(status: int, label: str) -> None:
    """Treat not-yet-available states as pending and other statuses as failures."""
    if status == 200:
        return

    if status in (0, 404, 408, 429) or 500 <= status <= 599:
        raise PendingError(f"{label} is not available yet (HTTP {status}).")

    raise NuGetError(f"{label} was rejected (HTTP {status}).")


def package_base(fetcher=fetch) -> str:
    """Discover the flat-container address from the service index instead of assuming it."""
    # WHY: Clients discover resources from the service index. https://learn.microsoft.com/nuget/api/overview
    status, content = fetcher(INDEX, {}, 30)
    require_response(status, "The NuGet service index")
    try:
        addresses = {item["@id"] for item in json.loads(content)["resources"]
                     if item.get("@type") == "PackageBaseAddress/3.0.0"}
    except (ValueError, KeyError, TypeError) as error:
        raise NuGetError("The NuGet service index is invalid.") from error

    parsed = urlsplit(next(iter(addresses))) if len(addresses) == 1 else None
    if parsed is None or parsed.scheme != "https" or parsed.hostname != "api.nuget.org" or parsed.query:
        raise NuGetError("The NuGet service index has no single expected package base address.")

    return next(iter(addresses)).rstrip("/") + "/"


def package_url(base: str, package: dict) -> str:
    """Return the flat-container URL of a package; IDs and versions are lowercase there."""
    # WHY: https://learn.microsoft.com/nuget/api/package-base-address-resource
    name, version = package["id"].lower(), package["version"].lower()
    return f"{base}{name}/{version}/{name}.{version}.nupkg"


def unused(package_ids: list[str], version: str, *, fetcher=fetch) -> dict[str, str]:
    """Return absent or present for each package ID at the version; unclear answers fail."""
    base = package_base(fetcher)
    states = {}
    for package_id in package_ids:
        status, _ = fetcher(package_url(base, {"id": package_id, "version": version}), {}, 30)
        if status not in (200, 404):
            # WHY: A timeout or an error cannot prove that an immutable version is still unused.
            raise NuGetError(f"Cannot establish the state of {package_id} on nuget.org (HTTP {status}).")

        states[package_id] = "absent" if status == 404 else "present"

    return states


def availability(directory: Path, inventory: list[dict], *, fetcher=fetch) -> list[dict]:
    """Return absent or matching for every package; conflicting bytes or unclear answers fail."""
    base = package_base(fetcher)
    states = []
    for package in inventory:
        status, content = fetcher(package_url(base, package), {}, 30)
        if status == 404:
            states.append({"id": package["id"], "state": "absent"})
        elif status == 200:
            if packages.canonical_payload(content) != packages.canonical_payload(directory / package["nupkg"]):
                raise NuGetError(f"nuget.org holds other bytes for {package['id']} {package['version']}.")

            states.append({"id": package["id"], "state": "matching"})
        else:
            # WHY: A timeout or an error cannot prove that an immutable version is still unused.
            raise NuGetError(f"Cannot establish the state of {package['id']} on nuget.org (HTTP {status}).")

    return states


def verify_signature(path: Path, timeout: float) -> None:
    """Verify a downloaded package's signatures with the SDK that the global.json next to it selects."""
    # WHY: The CLI "looks for a global.json file iteratively navigating the path upward from the current working
    # directory"; the readback directory holds a copy of the product's global.json, not this repository's.
    # https://learn.microsoft.com/dotnet/core/tools/global-json
    # https://learn.microsoft.com/dotnet/core/tools/dotnet-nuget-verify
    result = subprocess.run(["dotnet", "nuget", "verify", str(path), "--all"], cwd=path.parent, capture_output=True,
                            text=True, timeout=timeout, check=False)
    if result.returncode:
        raise NuGetError(f"The signature of {path.name} on nuget.org does not verify.")


def validate_symbol_probes(probes: list[dict]) -> None:
    """Require probes for the same symbol-store URL to agree on the checksum and exact bytes."""
    # WHY: The Portable PDB symbol-store key uses the GUID, and the PE checksum excludes the PDB ID. Readback
    # counts one response per URL, so all probes for that URL must agree on both checksum and full-file hash.
    # https://github.com/dotnet/symstore/blob/main/docs/specs/SSQP_Key_Conventions.md
    # https://github.com/dotnet/runtime/blob/main/docs/design/specs/PE-COFF.md#portable-pdb-checksum
    expected: dict[str, tuple[str, str]] = {}
    for probe in probes:
        digests = probe["checksum"], probe["sha256"]
        if expected.setdefault(probe["url"], digests) != digests:
            raise NuGetError(f"Two symbol probes of {probe['pdb']} expect different bytes at one URL.")


def require_symbol(probe: dict, fetcher=fetch) -> None:
    """Require the symbol server to serve exactly the probe's Portable PDB; raise PendingError while it does not."""
    # WHY: symbols.nuget.org answers 403 without a SymbolChecksum header and 200 with one (Doka.NestedSet 10.4.0,
    # Doka.Tabular 1.0.0-rc.1; MySql's fetch_remote_symbol sends the same header). It also served the PDB for a wrong
    # checksum value, so the served bytes are compared by SHA-256. The documentation does not describe the header.
    status, content = fetcher(probe["url"], {"SymbolChecksum": probe["checksum"]}, 30)
    require_response(status, probe["pdb"])
    require_symbol_bytes(probe, content)


def require_symbol_bytes(probe: dict, content: bytes) -> None:
    """Require the served Portable PDB to be byte-identical to the probe's."""
    if hashlib.sha256(content).hexdigest() != probe["sha256"]:
        raise NuGetError(f"The symbol server serves another {probe['pdb']}.")


def symbol_state(probe: dict, fetcher=fetch) -> str:
    """Return absent or matching for one Portable PDB; other bytes or unclear answers fail."""
    status, content = fetcher(probe["url"], {"SymbolChecksum": probe["checksum"]}, 30)
    if status == 404:
        return "absent"

    if status != 200:
        # WHY: A timeout or an error cannot prove that a symbol package is missing, and a push would replace an
        # available one. MySql's fetch_remote_symbol reports only 404 as absent and stops the preflight otherwise.
        raise NuGetError(f"Cannot establish the state of {probe['pdb']} on the symbol server (HTTP {status}).")

    require_symbol_bytes(probe, content)
    return "matching"


def pending_symbols(inventory: list[dict], probes: list[dict], *, fetcher=fetch) -> list[dict]:
    """Return the packages whose Portable PDBs the symbol server does not serve yet, in inventory order."""
    # WHY: nuget.org accepts a repeated symbol package and replaces an available one with it; it refuses one with 409
    # only while a symbol package of that version validates (NuGetGallery SymbolPackageUploadService). MySql therefore
    # asks the symbol server first and pushes only the symbol packages whose PDBs are missing.
    # https://github.com/NuGet/NuGetGallery/blob/main/src/NuGetGallery/Services/SymbolPackageUploadService.cs
    validate_symbol_probes(probes)
    pending = []
    for package in inventory:
        owned = [probe for probe in probes if probe["package"] == package["nupkg"]]
        if not owned:
            raise NuGetError(f"{package['nupkg']} has no symbol probe.")

        # WHY: Every probe is asked, so other bytes for one PDB fail also when another PDB of the package is absent
        # (MySql's observe_remote_symbols classifies every entry).
        states = [symbol_state(probe, fetcher) for probe in owned]
        if "absent" in states:
            pending.append(package)

    return pending


def readback(directory: Path, inventory: list[dict], probes: list[dict], work: Path, sdk: Path, *, fetcher=fetch,
             verifier=verify_signature, clock=time.monotonic, sleep=time.sleep,
             timeout: int = READBACK_SECONDS) -> dict:
    """Wait until nuget.org serves every package signed and byte-identical and every Portable PDB."""
    validate_symbol_probes(probes)
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sdk, work / "global.json")
    deadline = clock() + timeout
    verified, symbols, base = {}, set(), None
    while True:
        pending = []
        try:
            base = base or package_base(fetcher)
        except PendingError as error:
            pending.append(str(error))

        if base is not None:
            for package in inventory:
                if package["id"] in verified:
                    continue

                try:
                    status, content = fetcher(package_url(base, package), {}, 30)
                    require_response(status, package["id"])
                    if packages.canonical_payload(content) != packages.canonical_payload(directory / package["nupkg"]):
                        raise NuGetError(f"nuget.org serves other bytes for {package['id']}.")

                    if packages.SIGNATURE_ENTRY not in packages.archive_entries(content):
                        raise PendingError(f"The repository signature of {package['id']} is not there yet.")

                    public = work / package["nupkg"]
                    public.write_bytes(content)
                    verifier(public, 120)
                    verified[package["id"]] = hashlib.sha256(content).hexdigest()
                except PendingError as error:
                    pending.append(str(error))

            for probe in probes:
                if probe["url"] in symbols:
                    continue

                try:
                    require_symbol(probe, fetcher)
                    symbols.add(probe["url"])
                except PendingError as error:
                    pending.append(str(error))

        # WHY: Two assemblies can carry the same Portable PDB, which gives two probes with one URL.
        if len(verified) == len(inventory) and symbols == {probe["url"] for probe in probes}:
            return {"packages": [{"id": package["id"], "publicSha256": verified[package["id"]]}
                                 for package in inventory], "symbols": sorted(symbols)}

        if clock() >= deadline:
            raise PendingError("nuget.org did not serve every package and symbol before the deadline: "
                               + "; ".join(pending))

        sleep(max(0.0, min(POLL_SECONDS, deadline - clock())))
