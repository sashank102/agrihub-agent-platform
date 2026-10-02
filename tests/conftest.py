"""Shared fixtures: a local HTTP data server and a tiny soybean bundle built from tests/data."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from agrihub_fixtures import (
    DataServer,
    FixtureBundle,
    fixture_registry,
    publish_fixture_files,
)

from agrihub.configuration import MODEL_FIELDS
from agrihub_data.availability import clear_cache as clear_availability
from agrihub_data.build import build
from agrihub_data.bundle import close_bundles
from agrihub_data.fetch import fetch
from agrihub_data.registry import register_species, unregister_species


def _serve(root: Path) -> DataServer:
    server = DataServer(root)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def data_server(tmp_path: Path) -> Iterator[DataServer]:
    root = tmp_path / "server"
    root.mkdir()
    server = _serve(root)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session")
def fixture_bundle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FixtureBundle]:
    """Fetch the fixture sources over local HTTP and build a soybean bundle once."""
    root = tmp_path_factory.mktemp("fixture-server")
    data_dir = tmp_path_factory.mktemp("fixture-data")
    server = _serve(root)
    registry = fixture_registry(server.base_url)
    publish_fixture_files(registry, root)
    register_species(registry)
    try:
        fetched = fetch("soybean", "core", data_dir=data_dir, concurrency=4)
        assert fetched.ok, fetched.failed
        built = build("soybean", "core", data_dir=data_dir)
        yield FixtureBundle(data_dir, registry, fetched, built, list(server.requests))
    finally:
        unregister_species("soybean")
        close_bundles()
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session")
def heavy_bundle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FixtureBundle]:
    """Fetch every fixture source and build a heavy-tier soybean bundle once."""
    root = tmp_path_factory.mktemp("heavy-server")
    data_dir = tmp_path_factory.mktemp("heavy-data")
    server = _serve(root)
    registry = fixture_registry(server.base_url)
    publish_fixture_files(registry, root)
    register_species(registry)
    try:
        fetched = fetch("soybean", "heavy", data_dir=data_dir, concurrency=4)
        assert fetched.ok, fetched.failed
        built = build("soybean", "heavy", data_dir=data_dir)
        yield FixtureBundle(data_dir, registry, fetched, built, list(server.requests))
    finally:
        unregister_species("soybean")
        close_bundles()
        server.shutdown()
        server.server_close()


@pytest.fixture
def heavy_env(
    heavy_bundle: FixtureBundle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FixtureBundle]:
    """Point tools at the heavy fixture bundle and a fresh run directory."""
    register_species(heavy_bundle.registry)
    monkeypatch.setenv("AGRIHUB_DATA_DIR", str(heavy_bundle.data_dir))
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path / "runs"))
    clear_availability()
    try:
        yield heavy_bundle
    finally:
        unregister_species("soybean")
        close_bundles()
        clear_availability()


@pytest.fixture
def fixture_env(
    fixture_bundle: FixtureBundle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FixtureBundle]:
    """Point tools at the fixture bundle and a fresh run directory."""
    register_species(fixture_bundle.registry)
    monkeypatch.setenv("AGRIHUB_DATA_DIR", str(fixture_bundle.data_dir))
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path / "runs"))
    clear_availability()
    try:
        yield fixture_bundle
    finally:
        unregister_species("soybean")
        close_bundles()
        clear_availability()


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point every agent role at the scripted poster model so no test calls a model provider."""
    for name in MODEL_FIELDS:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("MODEL", "agrihub-fake:poster")
    return "agrihub-fake:poster"
