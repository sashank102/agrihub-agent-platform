"""Dry-run a study request: intake validation, SNP placement and a locus preview (fixed or LD windows).

Nothing here starts a run, creates a thread or writes an evidence store. The
study form shows the result as the server's view of what a run would do.
"""

from typing import Any

from agrihub.nodes.intake import check_study
from agrihub.nodes.locus_builder import ld_request, preview_loci
from agrihub.state import SnpStudy, StudyWarning
from agrihub_data.bundle import BundleMissingError


def preview_study(raw: Any) -> dict[str, Any]:
    """Validate a study as intake does and preview the loci its SNPs would form.

    Returns a dictionary with ``study_normalized`` (``None`` when the
    request does not validate), ``placed_snps``, ``warnings``, ``errors`` of
    ``{loc, message}``, a one-line ``detail``, and ``preview`` with the
    merged ``loci`` (fixed or LD windows, gene counts after per-locus capping) and
    the total number of distinct candidate ``genes``. Trait studies have no
    SNPs yet, so their preview is empty.
    """
    check = check_study(raw)
    warnings = list(check.warnings)
    loci: list[dict[str, Any]] = []
    if isinstance(check.study, SnpStudy) and not check.errors:
        study = check.study
        try:
            built, locus_warnings = preview_loci(
                study.species,
                study.assembly,
                check.placed,
                study.window.flank_bp,
                study.max_genes_per_locus,
                ld_request(study.model_dump(mode="json")),
            )
        except BundleMissingError as exc:
            warnings.append(StudyWarning(code="bundle_missing", message=f"gene counts are unavailable: {exc}"))
        else:
            warnings.extend(locus_warnings)
            loci = [locus.model_dump(mode="json") for locus in built]
    return {
        "study_normalized": check.study.model_dump(mode="json") if check.study is not None else None,
        "placed_snps": [snp.model_dump(mode="json") for snp in check.placed],
        "warnings": [warning.model_dump(exclude_none=True) for warning in warnings],
        "errors": [issue.model_dump() for issue in check.errors],
        "detail": check.detail,
        "preview": {"loci": loci, "genes": sum(int(locus["n_genes"]) for locus in loci)},
    }
