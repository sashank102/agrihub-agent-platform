"""External binaries the heavy-tier tools shell out to: PLINK2, Ensembl VEP and SnpEff.

Each locator returns ``None`` when the binary is not installed, so tools can
report an "unavailable" evidence gap instead of failing. Locations, in order:

- PLINK2: ``AGRIHUB_PLINK2`` when set, else ``<data_dir>/_tools/bin/plink2``, else ``plink2`` on PATH.
- VEP: ``AGRIHUB_VEP`` (a native ``vep`` script), else the Docker image
  ``AGRIHUB_VEP_IMAGE`` (default ``ensemblorg/ensembl-vep:release_116.2``)
  when Docker runs and the image is pulled.
- SnpEff: ``AGRIHUB_SNPEFF`` or ``snpEff`` on PATH.

``scripts/setup_heavy_tools.sh`` installs PLINK2 and pulls the VEP image.
Docker probes are cached for ``PROBE_TTL_SECONDS``.
"""

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agrihub_data.paths import data_root

DEFAULT_VEP_IMAGE = "ensemblorg/ensembl-vep:release_116.2"
PROBE_TTL_SECONDS = 60.0
_probe_lock = threading.Lock()
_probes: dict[str, tuple[float, bool]] = {}


@dataclass(frozen=True)
class VepRunner:
    """How to run VEP: a native script or a Docker image."""

    kind: Literal["native", "docker"]
    target: str
    """The ``vep`` executable path, or the Docker image name."""


def plink2_path() -> Path | None:
    """Return the PLINK2 executable, or ``None`` when it is not installed.

    A set ``AGRIHUB_PLINK2`` is the only candidate, so it can also switch PLINK2 off.
    """
    configured = os.environ.get("AGRIHUB_PLINK2")
    candidates = [Path(configured)] if configured else [data_root() / "_tools" / "bin" / "plink2"]
    found = shutil.which("plink2")
    if found and not configured:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def vep_runner() -> VepRunner | None:
    """Return how to run VEP, or ``None`` when neither a native VEP nor its Docker image is available."""
    native = os.environ.get("AGRIHUB_VEP") or shutil.which("vep")
    if native and Path(native).is_file():
        return VepRunner("native", native)
    image = vep_image()
    if shutil.which("docker") and _cached_probe(f"docker-image:{image}", lambda: _docker_has(image)):
        return VepRunner("docker", image)
    return None


def vep_image() -> str:
    """Return the configured VEP Docker image."""
    return os.environ.get("AGRIHUB_VEP_IMAGE") or DEFAULT_VEP_IMAGE


def snpeff_path() -> Path | None:
    """Return the SnpEff launcher, or ``None``."""
    configured = os.environ.get("AGRIHUB_SNPEFF") or shutil.which("snpEff")
    if configured and Path(configured).is_file():
        return Path(configured)
    return None


def reset_probes() -> None:
    """Forget cached Docker probes (tests, or after installing an image)."""
    with _probe_lock:
        _probes.clear()


def _cached_probe(key: str, probe: Callable[[], bool]) -> bool:
    now = time.monotonic()
    with _probe_lock:
        cached = _probes.get(key)
        if cached is not None and now - cached[0] < PROBE_TTL_SECONDS:
            return cached[1]
    result = probe()
    with _probe_lock:
        _probes[key] = (now, result)
    return result


def _docker_has(image: str) -> bool:
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0
