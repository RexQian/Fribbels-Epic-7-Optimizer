"""GitHub Actions helper for one draft release; never overwrites a published release."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def command(*args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if result.returncode and not allow_failure:
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-500:]}")
    return result


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _verify_offline_data(data_root: Path) -> None:
    if not (data_root / "cache/herodata.json").is_file() or not (data_root / "cache/artifactdata.json").is_file():
        raise RuntimeError("Packaged hero/artifact cache is missing")
    command("node", "scripts/verify-optimizer-offline.cjs", ".", str(data_root))


def mac_zip_arch(name: str, version: str) -> str | None:
    match = re.fullmatch(rf"FribbelsE7Optimizer-{re.escape(version)}-(arm64-)?mac\.zip", name)
    return ("arm64" if match.group(1) else "x64") if match else None


def mac_dmg_arch(name: str, version: str) -> str | None:
    match = re.fullmatch(rf"FribbelsE7Optimizer-{re.escape(version)}(-arm64)?\.dmg", name)
    return ("arm64" if match.group(1) else "x64") if match else None


def _mac_zip_packages(root: Path, version: str) -> dict[str, Path]:
    packages: dict[str, Path] = {}
    for path in root.iterdir():
        if not path.is_file() or path.suffix.lower() != ".zip":
            continue
        arch = mac_zip_arch(path.name, version)
        if arch is None or arch in packages:
            raise RuntimeError(f"Unexpected macOS ZIP: {path.name}")
        packages[arch] = path
    if set(packages) != {"x64", "arm64"}:
        raise RuntimeError("Both x64 and arm64 macOS ZIP packages are required")
    return packages


def _mac_dmg_packages(root: Path, version: str) -> dict[str, Path]:
    packages: dict[str, Path] = {}
    for path in root.iterdir():
        if not path.is_file() or path.suffix.lower() != ".dmg":
            continue
        arch = mac_dmg_arch(path.name, version)
        if arch is None or arch in packages:
            raise RuntimeError(f"Unexpected macOS DMG: {path.name}")
        packages[arch] = path
    if set(packages) != {"x64", "arm64"}:
        raise RuntimeError("Both x64 and arm64 macOS DMG packages are required")
    return packages


def _verify_mac_info(info: dict, version: str) -> str:
    if info.get("CFBundleShortVersionString") != version or info.get("CFBundleVersion") != version:
        raise RuntimeError("Packaged macOS app version differs from frozen version")
    executable = info.get("CFBundleExecutable")
    if not isinstance(executable, str) or not re.fullmatch(r"[A-Za-z0-9_. -]+", executable):
        raise RuntimeError("Packaged macOS executable is invalid")
    return executable


def _verify_mac_cpu(header: bytes, arch: str, name: str) -> None:
    if len(header) != 8:
        raise RuntimeError("Packaged macOS executable is truncated")
    if header[:4] == b"\xcf\xfa\xed\xfe":
        cpu = struct.unpack("<I", header[4:8])[0]
    elif header[:4] == b"\xfe\xed\xfa\xcf":
        cpu = struct.unpack(">I", header[4:8])[0]
    else:
        raise RuntimeError("Packaged executable is not a thin Mach-O binary")
    if cpu != {"x64": 0x01000007, "arm64": 0x0100000c}[arch]:
        raise RuntimeError(f"macOS package architecture differs: {name}")


def _verify_mac_dmg(package: Path, version: str, arch: str) -> None:
    directory = Path(tempfile.mkdtemp(prefix="e7-offline-mac-dmg-"))
    mounted = directory / "mounted"
    mounted.mkdir()
    attached = subprocess.run(
        ["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", "-mountpoint", str(mounted), str(package)],
        capture_output=True, check=False, timeout=120)
    if attached.returncode:
        shutil.rmtree(directory, ignore_errors=True)
        raise RuntimeError(f"Cannot mount macOS DMG read-only: {package.name}: {attached.stderr[-500:]!r}")
    device = str(mounted)
    failure: BaseException | None = None
    try:
        entries = plistlib.loads(attached.stdout).get("system-entities", [])
        devices = [entry["dev-entry"] for entry in entries if entry.get("mount-point") == str(mounted)
                   and isinstance(entry.get("dev-entry"), str)]
        if len(devices) != 1:
            raise RuntimeError("DMG mounted without a unique device")
        device = devices[0]
        apps = [path for path in mounted.iterdir() if path.is_dir() and path.suffix == ".app"]
        if len(apps) != 1:
            raise RuntimeError("DMG must contain one root .app")
        contents = apps[0] / "Contents"
        executable = _verify_mac_info(plistlib.loads((contents / "Info.plist").read_bytes()), version)
        with (contents / "MacOS" / executable).open("rb") as stream:
            _verify_mac_cpu(stream.read(8), arch, package.name)
        _verify_offline_data(contents / "data")
    except BaseException as exc:
        failure = exc
        raise
    finally:
        detach_error = ""
        for attempt in range(3):
            args = ["hdiutil", "detach", device] if attempt < 2 else ["hdiutil", "detach", "-force", device]
            try:
                detached = subprocess.run(args, capture_output=True, check=False, timeout=30)
                if detached.returncode == 0:
                    shutil.rmtree(directory, ignore_errors=True)
                    break
                detach_error = repr(detached.stderr[-500:])
            except subprocess.TimeoutExpired:
                detach_error = "timed out"
            if attempt < 2:
                time.sleep(2)
        else:
            message = f"Cannot detach DMG device {device} ({detach_error}); mount retained at {mounted}"
            if failure is not None:
                failure.args = (*failure.args, message)
            else:
                raise RuntimeError(message)


def _verify_mac_zip(package: Path, version: str, arch: str) -> None:
    with zipfile.ZipFile(package) as archive:
        members = archive.infolist()
        names = [item.filename for item in members]
        if len(names) != len(set(names)):
            raise RuntimeError("Duplicate ZIP member")
        for item in members:
            path = Path(item.filename)
            if item.filename.startswith("/") or "\\" in item.filename or ".." in path.parts:
                raise RuntimeError("Unsafe macOS ZIP path")
            if "/Contents/data/" in item.filename and (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("Symlink in macOS ZIP")
        infos = [name for name in names if re.fullmatch(r"[^/]+\.app/Contents/Info\.plist", name)]
        if len(infos) != 1:
            raise RuntimeError("ZIP must contain one root .app/Contents/Info.plist")
        prefix = infos[0][:-len("Info.plist")]
        info = plistlib.loads(archive.read(infos[0]))
        executable = _verify_mac_info(info, version)
        binary = prefix + "MacOS/" + executable
        if binary not in names:
            raise RuntimeError("Packaged macOS executable is missing")
        with archive.open(binary) as stream:
            header = stream.read(8)
        _verify_mac_cpu(header, arch, package.name)
        data_prefix = prefix + "data/"
        with tempfile.TemporaryDirectory(prefix="e7-offline-mac-zip-") as directory:
            data_root = Path(directory) / "data"
            for item in members:
                if not item.filename.startswith(data_prefix) or item.is_dir():
                    continue
                relative = Path(item.filename[len(data_prefix):])
                if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                    raise RuntimeError("Unsafe packaged data path")
                target = data_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            _verify_offline_data(data_root)


def verify_packaged_cache(root: Path) -> None:
    """Run the real offline loader against data extracted from built packages."""
    platform = os.environ["PLATFORM"]
    if platform == "macos":
        version = os.environ["RELEASE_TAG"].removeprefix("v")
        for arch, package in _mac_zip_packages(root, version).items():
            _verify_mac_zip(package, version, arch)
        for arch, package in _mac_dmg_packages(root, version).items():
            _verify_mac_dmg(package, version, arch)
    elif platform == "windows":
        packages = sorted(root.glob("*.zip"))
        if not packages:
            raise RuntimeError("No Windows ZIP package to inspect")
        for package in packages:
            with tempfile.TemporaryDirectory(prefix="e7-offline-zip-") as directory:
                data_root = Path(directory) / "data"
                with zipfile.ZipFile(package) as archive:
                    members = archive.namelist()
                    candidates = [name for name in members if name.endswith("data/cache/herodata.json")]
                    if len(candidates) != 1:
                        raise RuntimeError(f"ZIP has no unique packaged data root: {package.name}")
                    prefix = candidates[0][:-len("data/cache/herodata.json")]
                    for member in members:
                        if not member.startswith(prefix + "data/") or member.endswith("/"):
                            continue
                        relative = Path(member[len(prefix + "data/"):])
                        if relative.is_absolute() or ".." in relative.parts:
                            raise RuntimeError("Unsafe packaged data path")
                        target = data_root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
                _verify_offline_data(data_root)
    else:
        raise RuntimeError("Invalid package platform")


def provenance(root: Path, platform: str, tag: str, run_id: str, attempt: str,
               commit: str) -> dict:
    if platform == "macos":
        version = tag.removeprefix("v")
        files = list(_mac_zip_packages(root, version).values()) + list(_mac_dmg_packages(root, version).values())
    else:
        files = sorted(path for path in root.iterdir() if path.is_file() and path.suffix.lower() == ".exe")
    if not files:
        raise RuntimeError(f"Missing {platform} installation package")
    return {"schema_version": 1, "platform": platform, "tag": tag,
            "run_id": run_id, "run_attempt": attempt, "commit": commit,
            "assets": [{"name": path.name, "sha256": digest(path), "size": path.stat().st_size}
                       for path in files]}


def record(root: Path) -> None:
    platform = os.environ["PLATFORM"]
    if platform not in ("macos", "windows"):
        raise RuntimeError("Invalid platform")
    result = provenance(root, platform, os.environ["RELEASE_TAG"], os.environ["RUN_ID"],
                        os.environ["RUN_ATTEMPT"], command("git", "rev-parse", "HEAD").stdout.strip())
    (root / f"build-{platform}.json").write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")


def verify(root: Path) -> list[Path]:
    tag, run_id, attempt = (os.environ[key] for key in ("RELEASE_TAG", "RUN_ID", "RUN_ATTEMPT"))
    commit = command("git", "rev-parse", "HEAD").stdout.strip()
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-offline(?:\.[0-9A-Za-z.-]+)?", tag):
        raise RuntimeError("Invalid offline tag")
    expected_commit = os.environ.get("CANDIDATE_COMMIT", commit)
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit) or expected_commit != commit:
        raise RuntimeError("Build commit differs from the reviewed candidate")
    files: list[Path] = []
    names: set[str] = set()
    mac_arches: dict[str, set[str]] = {"zip": set(), "dmg": set()}
    for platform in ("macos", "windows"):
        meta_path = root / f"build-{platform}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if any(str(meta.get(key)) != value for key, value in (
            ("platform", platform), ("tag", tag), ("run_id", run_id),
            ("run_attempt", attempt), ("commit", commit)
        )):
            raise RuntimeError(f"Build provenance differs: {platform}")
        if not isinstance(meta.get("assets"), list) or not meta["assets"]:
            raise RuntimeError(f"Build assets missing: {platform}")
        for item in meta["assets"]:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in names:
                raise RuntimeError("Duplicate or unsafe build artifact name")
            if platform == "macos":
                kind = "zip" if name.endswith(".zip") else "dmg"
                arch = (mac_zip_arch if kind == "zip" else mac_dmg_arch)(name, tag.removeprefix("v"))
                if arch is None or arch in mac_arches[kind]:
                    raise RuntimeError("Build artifact platform mismatch")
                mac_arches[kind].add(arch)
            elif not name.lower().endswith(".exe"):
                raise RuntimeError("Build artifact platform mismatch")
            path = root / name
            if not path.is_file() or path.stat().st_size != item.get("size") or digest(path) != item.get("sha256"):
                raise RuntimeError(f"Build artifact differs: {name}")
            names.add(name)
            files.append(path)
        files.append(meta_path)
    if any(arches != {"x64", "arm64"} for arches in mac_arches.values()):
        raise RuntimeError("Draft is missing an x64 or arm64 macOS ZIP or DMG")
    return files


def ensure_tag(tag: str, commit: str) -> None:
    """Create the lightweight tag only after both packages verify; never move it."""
    repo = os.environ.get("GITHUB_REPOSITORY", "RexQian/Fribbels-Epic-7-Optimizer")
    if repo != "RexQian/Fribbels-Epic-7-Optimizer":
        raise RuntimeError("Unexpected release repository")
    endpoint = f"repos/{repo}/git/ref/tags/{tag}"
    found = command("gh", "api", endpoint, allow_failure=True)
    if found.returncode == 0:
        value = json.loads(found.stdout)
        if value.get("object", {}).get("type") != "commit" or value.get("object", {}).get("sha") != commit:
            raise RuntimeError("Existing tag points to another commit")
        return
    if "404" not in found.stderr and "not found" not in found.stderr.lower():
        raise RuntimeError(f"Cannot inspect release tag: {found.stderr[-300:]}")
    created = command("gh", "api", "--method", "POST", f"repos/{repo}/git/refs",
                      "-f", f"ref=refs/tags/{tag}", "-f", f"sha={commit}", allow_failure=True)
    if created.returncode != 0:
        # A concurrent run may have created it; re-read and accept only the same SHA.
        found = command("gh", "api", endpoint)
        value = json.loads(found.stdout)
        if value.get("object", {}).get("type") != "commit" or value.get("object", {}).get("sha") != commit:
            raise RuntimeError("Concurrent release tag points to another commit")


def publish(root: Path) -> None:
    files = verify(root)
    tag = os.environ["RELEASE_TAG"]
    commit = os.environ["CANDIDATE_COMMIT"]
    found = command("gh", "release", "view", tag, "--json", "isDraft,tagName,assets", allow_failure=True)
    if found.returncode == 0:
        value = json.loads(found.stdout)
        if value.get("isDraft") is not True or value.get("tagName") != tag:
            raise RuntimeError("Existing release is published or belongs to another tag")
        attached = {item.get("name") for item in value.get("assets", [])}
        expected = {path.name for path in files}
        if not attached <= expected:
            raise RuntimeError("Existing draft contains unreviewed extra attachments")
        existing = root / "existing-release-assets"
        existing.mkdir(exist_ok=True)
        if attached:
            command("gh", "release", "download", tag, "--dir", str(existing), "--pattern", "*")
        missing = []
        for path in files:
            old = existing / path.name
            if old.exists():
                if not old.is_file() or digest(old) != digest(path):
                    raise RuntimeError(f"Existing draft asset changed: {path.name}")
            else:
                missing.append(str(path))
        ensure_tag(tag, commit)
        if missing:
            command("gh", "release", "upload", tag, *missing)
    else:
        if "not found" not in found.stderr.lower() and "could not find" not in found.stderr.lower():
            raise RuntimeError(f"Cannot determine whether draft exists: {found.stderr[-300:]}")
        ensure_tag(tag, commit)
        command("gh", "release", "create", tag, *(str(path) for path in files), "--draft",
                "--verify-tag", "--title", tag, "--notes", "Release notes pending human review.")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("record", "verify", "publish", "verify-package"):
        raise SystemExit("Usage: offline-draft.py record|verify|publish|verify-package ASSET_DIR")
    root = Path(sys.argv[2]).resolve()
    if sys.argv[1] == "record":
        record(root)
    elif sys.argv[1] == "verify":
        verify(root)
    elif sys.argv[1] == "verify-package":
        verify_packaged_cache(root)
    else:
        publish(root)
