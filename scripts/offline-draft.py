"""GitHub Actions helper for one draft release; never overwrites a published release."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


def verify_packaged_cache(root: Path) -> None:
    """Run the real offline loader against data extracted from built packages."""
    platform = os.environ["PLATFORM"]
    if platform == "macos":
        packages = sorted(root.glob("*.dmg"))
        if not packages:
            raise RuntimeError("No macOS disk image to inspect")
        for package in packages:
            with tempfile.TemporaryDirectory(prefix="e7-offline-dmg-") as directory:
                mount = Path(directory) / "mounted"
                mount.mkdir()
                command("hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint",
                        str(mount), str(package))
                try:
                    roots = list(mount.glob("*.app/Contents/data"))
                    if len(roots) != 1:
                        raise RuntimeError(f"Disk image has no unique packaged data root: {package.name}")
                    _verify_offline_data(roots[0])
                finally:
                    command("hdiutil", "detach", str(mount))
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
    extensions = (".dmg", ".pkg") if platform == "macos" else (".exe",)
    files = sorted(path for path in root.iterdir() if path.is_file() and path.suffix.lower() in extensions)
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
            if (platform == "macos" and not name.lower().endswith((".dmg", ".pkg"))) or (
                platform == "windows" and not name.lower().endswith(".exe")
            ):
                raise RuntimeError("Build artifact platform mismatch")
            path = root / name
            if not path.is_file() or path.stat().st_size != item.get("size") or digest(path) != item.get("sha256"):
                raise RuntimeError(f"Build artifact differs: {name}")
            names.add(name)
            files.append(path)
        files.append(meta_path)
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
