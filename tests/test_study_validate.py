"""POST /studies/validate dry-runs intake on the fixture bundle without starting anything."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agrihub_fixtures import FixtureBundle
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_platform.api.dependencies import require_ready
from agent_platform.api.routes import studies as studies_routes
from agent_platform.core.settings import Settings

STUDY = {
    "mode": "snps",
    "species": "soybean",
    "assembly": "a2",
    "trait_text": "plant height",
    "snps": [
        {"raw": "S5_2899164"},
        {"raw": "Chr18:9263941"},
        {"raw": "S18_9300000", "chrom": "Gm18", "pos": 9_300_000},
        {"raw": "S18_99999999"},
    ],
}


def _app(auth_mode: str) -> FastAPI:
    app = FastAPI()
    app.include_router(studies_routes.router)
    app.state.settings = Settings(
        ENVIRONMENT="test",
        AUTH_MODE=auth_mode,
        API_KEY_PEPPER="test-pepper-0123456789",
        DATABASE_URI="postgresql://agent_platform:agent_platform@localhost:5432/agent_platform",
        _env_file=None,
    )

    async def record_auth_failure(reason: str) -> None:
        return None

    app.state.accounts = SimpleNamespace(record_auth_failure=record_auth_failure)
    app.dependency_overrides[require_ready] = lambda: None
    return app


def _validate(body: Any, auth_mode: str = "disabled") -> tuple[int, Any]:
    async def scenario() -> tuple[int, Any]:
        async with AsyncClient(transport=ASGITransport(app=_app(auth_mode)), base_url="http://test") as client:
            response = await client.post("/studies/validate", json=body)
        return response.status_code, response.json()

    return asyncio.run(scenario())


@pytest.fixture
def run_root(fixture_env: FixtureBundle, tmp_path: Path) -> Path:
    return tmp_path / "runs"


def test_validate_places_snps_and_previews_loci_without_writing(run_root: Path):
    status, body = _validate(STUDY)
    assert status == 200
    assert body["errors"] == []
    assert body["study_normalized"]["assembly"] == "Wm82.a2.v1"
    assert [(snp["raw"], snp["chrom"], snp["pos"]) for snp in body["placed_snps"]] == [
        ("S5_2899164", "Gm05", 2_899_164),
        ("Chr18:9263941", "Gm18", 9_263_941),
        ("S18_9300000", "Gm18", 9_300_000),
    ]
    assert [warning["code"] for warning in body["warnings"]] == ["out_of_bounds"]
    assert body["warnings"][0]["snp"] == "S18_99999999"
    loci = body["preview"]["loci"]
    assert [(locus["locus_id"], locus["chrom"]) for locus in loci] == [("L1", "Gm05"), ("L2", "Gm18")]
    assert loci[1]["merged_from"] == ["Chr18:9263941", "S18_9300000"]
    assert all(locus["n_genes"] > 0 for locus in loci)
    assert body["preview"]["genes"] == sum(locus["n_genes"] for locus in loci)
    assert body["detail"] == "3 valid SNPs of 4 submitted; 1 warnings"
    assert not run_root.exists()


def test_validate_reports_located_errors_for_unrunnable_studies(run_root: Path):
    status, malformed = _validate({**STUDY, "snps": [{"raw": "S5_1", "chrom": "5"}], "window": {"flank_bp": -1}})
    assert status == 200
    assert malformed["study_normalized"] is None and malformed["placed_snps"] == []
    assert sorted(error["loc"] for error in malformed["errors"]) == [["snps", 0], ["window", "flank_bp"]]
    assert malformed["preview"] == {"loci": [], "genes": 0}

    _, unknown = _validate({**STUDY, "assembly": "Wm82.a9"})
    assert [error["loc"] for error in unknown["errors"]] == [["assembly"]]

    _, unplaced = _validate({**STUDY, "snps": [{"raw": "S99_1"}, {"raw": "S18_99999999"}]})
    assert unplaced["study_normalized"]["species"] == "soybean"
    assert [error["loc"] for error in unplaced["errors"]] == [["snps"]]
    assert {warning["code"] for warning in unplaced["warnings"]} == {"unresolved_marker", "out_of_bounds"}

    _, trait = _validate({"mode": "trait", "species": "soybean", "assembly": "Wm82.a2.v1", "trait_text": "height"})
    assert trait["errors"] == [] and trait["preview"] == {"loci": [], "genes": 0}
    assert not run_root.exists()


def test_validate_window_warning_and_auth(run_root: Path):
    _, wide = _validate({**STUDY, "window": {"mode": "fixed", "flank_bp": 400_000}})
    assert "window_exceeds_ld" in {warning["code"] for warning in wide["warnings"]}
    status, _ = _validate(STUDY, auth_mode="api_key")
    assert status == 401
