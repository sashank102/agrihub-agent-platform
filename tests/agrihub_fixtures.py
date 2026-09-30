"""Helpers for offline data tests: a local HTTP data server and the fixture soybean registry."""

import gzip
import os
import re
import shutil
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from agrihub_data.build import BuildReport
from agrihub_data.fetch import FetchReport
from agrihub_data.registry import Source, SpeciesRegistry

DATA = Path(__file__).parent / "data"
BUNDLE_DATA = DATA / "bundle"


class DataServer(ThreadingHTTPServer):
    """Serves a directory with Range support and records every request."""

    daemon_threads = True

    def __init__(self, root: Path) -> None:
        super().__init__(("127.0.0.1", 0), partial(RangeHandler, directory=str(root)))
        self.root = root
        self.requests: list[tuple[str, str | None]] = []
        self.cut_once: set[str] = set()
        """Paths whose next full response is cut after half the body."""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class RangeHandler(SimpleHTTPRequestHandler):
    server: DataServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        path = self.translate_path(self.path)
        self.server.requests.append((self.path, self.headers.get("Range")))
        if os.path.isdir(path) or not os.path.exists(path):
            super().do_GET()
            return
        data = Path(path).read_bytes()
        start = 0
        match = re.fullmatch(r"bytes=(\d+)-", self.headers.get("Range") or "")
        if match:
            start = int(match.group(1))
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", "Wed, 01 Oct 2025 00:00:00 GMT")
        self.end_headers()
        if not match and self.path in self.server.cut_once:
            self.server.cut_once.discard(self.path)
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)


def packaged_soybean() -> SpeciesRegistry:
    text = (resources.files("agrihub_data") / "registry" / "soybean.yaml").read_text(encoding="utf-8")
    return SpeciesRegistry.model_validate(yaml.safe_load(text))


def fixture_registry(base_url: str) -> SpeciesRegistry:
    """Return the soybean registry with sources pointing at the local fixture server."""
    text = (BUNDLE_DATA / "sources.yaml").read_text(encoding="utf-8").replace("{base}", base_url)
    sources = [Source.model_validate(raw) for raw in yaml.safe_load(text)["sources"]]
    return packaged_soybean().model_copy(update={"sources": sources})


def publish_fixture_files(registry: SpeciesRegistry, root: Path) -> None:
    """Copy tests/data/bundle/files to the server root, gzipping files served as ``.gz``."""
    for source_dir in sorted((BUNDLE_DATA / "files").iterdir()):
        source = next(item for item in registry.sources if item.id == source_dir.name)
        templates = [file.path for file in source.files]
        if source.collection is not None:
            templates.extend(file.path for file in source.collection.files)
        patterns = [re.escape(template).replace(r"\{entry\}", "[^/]+") for template in templates]
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source_dir).as_posix()
            target = root / source.id / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if any(re.fullmatch(pattern, relative + ".gz") for pattern in patterns):
                with gzip.GzipFile(target.with_name(target.name + ".gz"), "wb", mtime=0) as handle:
                    handle.write(path.read_bytes())
            else:
                shutil.copyfile(path, target)


@dataclass
class FixtureBundle:
    data_dir: Path
    registry: SpeciesRegistry
    fetch_report: FetchReport
    build_report: BuildReport
    requests: list[tuple[str, str | None]] = field(default_factory=list)
