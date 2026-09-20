from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class ComponentState:
    component_id: str
    name: str
    installed: bool
    detail: str


class DependencyManager:
    def __init__(self, manifest_path: Path, data_root: Path, tools_root: Path | None = None):
        self.manifest_path = Path(manifest_path)
        self.data_root = Path(data_root).resolve()
        self.tools_root = Path(tools_root).resolve() if tools_root else self.data_root / "tools"
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.components = {item["id"]: item for item in payload["components"]}

    def component_root(self, component_id: str) -> Path:
        item = self.components[component_id]
        relative = Path(item["destination"])
        parts = relative.parts
        if relative.is_absolute() or len(parts) < 2 or any(part in {".", ".."} for part in parts):
            raise ValueError(f"拒绝访问组件目录之外的路径：{relative}")
        if parts[0] == "tools":
            base = self.tools_root
            child = Path(*parts[1:])
        else:
            base = self.data_root
            child = relative
        destination = (base / child).resolve()
        if not destination.is_relative_to(base) or destination == base:
            raise ValueError(f"拒绝访问组件目录之外的路径：{relative}")
        return destination

    def _ensure_writable(self, destination: Path) -> None:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=destination.parent):
                pass
        except OSError as exc:
            raise PermissionError(
                f"安装目录没有写入权限：{destination.parent}。请将软件解压到可写目录后重试。"
            ) from exc

    def state(self, component_id: str) -> ComponentState:
        item = self.components[component_id]
        root = self.component_root(component_id)
        patterns = item.get("expected", [])
        installed = bool(patterns) and all(any(root.glob(pattern)) for pattern in patterns)
        return ComponentState(
            component_id,
            item["name"],
            installed,
            str(root) if installed else f"未安装；目标：{root}",
        )

    def states(self) -> list[ComponentState]:
        return [self.state(component_id) for component_id in self.components]

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _download(url: str, destination: Path, expected_hash: str, progress: ProgressCallback) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "VideoSubtitleToolkit/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as stream:
            total = int(response.headers.get("Content-Length", "0"))
            downloaded = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                stream.write(block)
                downloaded += len(block)
                progress(downloaded, total, destination.name)
        actual_hash = DependencyManager._sha256(destination)
        if expected_hash and actual_hash.lower() != expected_hash.lower():
            destination.unlink(missing_ok=True)
            raise ValueError(f"SHA-256 校验失败：{destination.name}")

    @staticmethod
    def _extract_zip(archive: Path, destination: Path) -> None:
        destination = destination.resolve()
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = (destination / member.filename).resolve()
                if os.path.commonpath([destination, target]) != str(destination):
                    raise ValueError(f"压缩包包含不安全路径：{member.filename}")
            bundle.extractall(destination)

    @staticmethod
    def _github_asset(item: dict) -> tuple[str, str]:
        api = f"https://api.github.com/repos/{item['repository']}/releases/{item.get('release', 'latest')}"
        request = urllib.request.Request(
            api,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "VideoSubtitleToolkit/1.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
        asset = next((value for value in release["assets"] if value["name"] == item["asset"]), None)
        if not asset:
            raise RuntimeError(f"GitHub Release 中未找到：{item['asset']}")
        digest = str(asset.get("digest") or "")
        expected_hash = digest.removeprefix("sha256:")
        if len(expected_hash) != 64:
            raise RuntimeError("GitHub Release 未提供可验证的 SHA-256，已停止下载")
        return asset["browser_download_url"], expected_hash

    def install(self, component_id: str, progress: ProgressCallback | None = None) -> ComponentState:
        item = self.components[component_id]
        progress = progress or (lambda downloaded, total, label: None)
        destination = self.component_root(component_id)
        self._ensure_writable(destination)
        staging = Path(tempfile.mkdtemp(prefix=f".{component_id}-", dir=destination.parent))
        archive = staging.parent / f".{component_id}.download"
        try:
            if item["kind"] == "files":
                for entry in item["files"]:
                    target = staging / entry["name"]
                    self._download(entry["url"], target, entry["sha256"], progress)
            else:
                if item["kind"] == "github_zip":
                    url, expected_hash = self._github_asset(item)
                else:
                    url, expected_hash = item["url"], item.get("sha256", "")
                self._download(url, archive, expected_hash, progress)
                if item["kind"] in {"zip", "github_zip"}:
                    self._extract_zip(archive, staging)
                elif item["kind"] == "file":
                    target = staging / item["target"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    archive.replace(target)
                else:
                    raise ValueError(f"不支持的组件类型：{item['kind']}")
                archive.unlink(missing_ok=True)
            patterns = item.get("expected", [])
            if not patterns or not all(any(staging.glob(pattern)) for pattern in patterns):
                raise RuntimeError(f"安装内容不完整：{item['name']}")
            backup = destination.with_name(destination.name + ".old")
            if backup.exists():
                shutil.rmtree(backup)
            if destination.exists():
                destination.replace(backup)
            staging.replace(destination)
            if backup.exists():
                shutil.rmtree(backup)
            return self.state(component_id)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            archive.unlink(missing_ok=True)

    def remove(self, component_id: str) -> None:
        destination = self.component_root(component_id)
        shutil.rmtree(destination, ignore_errors=True)
