"""Fake-LLM recordings pin heavy domains so the golden fixture ignores the host."""

import pytest

from agrihub_data import availability, external


def test_pin_hides_heavy_domains_even_when_binaries_exist():
    availability.pin_heavy_domains(False)
    try:
        status = availability.domain_status("soybean")
    finally:
        availability.pin_heavy_domains(None)
    if "ld" not in status:
        pytest.skip("soybean registry has no ld domain")
    assert status["ld"].available is False
    assert status["variant_consequence"].available is False


@pytest.mark.binaries
def test_unpinned_heavy_domains_follow_the_host():
    if external.plink2_path() is None or external.vep_runner() is None:
        pytest.skip("needs PLINK2 and VEP")
    availability.pin_heavy_domains(None)
    status = availability.domain_status("soybean")
    assert status["ld"].available is True
    assert status["variant_consequence"].available is True
