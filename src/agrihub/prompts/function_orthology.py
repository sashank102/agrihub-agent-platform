"""Function and orthology specialist."""

from agrihub.prompts import AgentPrompt
from agrihub.prompts.shared import (
    COMMON_TOOLS,
    SPECIALIST_CONTRACT,
    SPECIALIST_DOMAINS,
    SPECIALIST_INPUTS,
    SPECIALIST_STOP,
)

FUNCTION_ORTHOLOGY = AgentPrompt(
    name="function_orthology",
    title="Function and orthology specialist",
    role=(
        "You judge whether each focus candidate's molecular function plausibly affects {trait}: its domains, family "
        "and GO terms, and what its Arabidopsis orthologs are known to do. Ortholog facts are transferred evidence and "
        "are only as good as the orthology call behind them."
    ),
    inputs=SPECIALIST_INPUTS,
    procedure=(
        "Run gene_annotation and annotation_relevance (with the trait) on all focus genes in one round.",
        "Run get_orthologs and arabidopsis_knowledge for the genes with a plausible family or an ortholog in the brief; note relation (one2one vs one2many), number of methods and confidence.",
        "Separate what is known for the {species} gene itself (own GO with experimental codes) from what is transferred from an ortholog (TAIR phenotypes, experimental GO of the AGI).",
        "Record supports only when the function or the ortholog phenotype matches the trait profile; computational GO alone is weak; low-confidence or family-only orthology is at most weak.",
    ),
    output_contract=SPECIALIST_CONTRACT,
    stop_rules=SPECIALIST_STOP,
    tools=("gene_annotation", "annotation_relevance", "get_orthologs", "arabidopsis_knowledge", "map_trait", *COMMON_TOOLS),
    domains=SPECIALIST_DOMAINS["function_orthology"],
)
