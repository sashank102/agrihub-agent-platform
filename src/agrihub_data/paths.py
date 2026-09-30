"""On-disk layout of one species under ``AGRIHUB_DATA_DIR``.

::

    <data_dir>/<species>/
        raw/<source_id>/<file>     downloads, with ``.part`` while in flight
        manifest.json              url, size, sha256 and license per file
        bundle.duckdb              the built bundle, opened read-only by tools
"""

from dataclasses import dataclass
from pathlib import Path

from agent_platform.core.settings import get_data_paths

BUNDLE_NAME = "bundle.duckdb"
MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class SpeciesPaths:
    """Paths of one species directory."""

    root: Path

    @property
    def raw(self) -> Path:
        """Return the download directory."""
        return self.root / "raw"

    @property
    def manifest(self) -> Path:
        """Return the fetch manifest."""
        return self.root / MANIFEST_NAME

    @property
    def bundle(self) -> Path:
        """Return the built DuckDB bundle."""
        return self.root / BUNDLE_NAME

    def source_dir(self, source_id: str) -> Path:
        """Return the download directory of one source."""
        return self.raw / source_id


def data_root(data_dir: Path | str | None = None) -> Path:
    """Return ``data_dir`` or the configured ``AGRIHUB_DATA_DIR``."""
    return Path(data_dir) if data_dir is not None else get_data_paths().data_dir


def species_paths(species: str, data_dir: Path | str | None = None) -> SpeciesPaths:
    """Return the directory layout of one species."""
    return SpeciesPaths(data_root(data_dir) / species)
