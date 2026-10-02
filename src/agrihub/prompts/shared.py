"""Output contract, stop rules and inputs shared by the five specialists."""

SPECIALIST_INPUTS = (
    "The assignment message: your focus genes (with locus, distance to the nearest SNP, defline and provisional score), focus loci (region and lead SNP), the orchestrator's instructions and rationale, and findings already recorded on these genes.",
    "Evidence the harvest already stored for these genes; fetch it with get_evidence when you need the details behind an E<n> alias.",
)
SPECIALIST_CONTRACT = (
    "Record each claim with record_finding(target, claim, stance, strength, evidence_ids): target is a focus gene id (or a locus id such as L1), claim is one or two sentences, evidence_ids are the E<n> aliases that support exactly this claim.",
    "stance is relative to the trait: supports (evidence links the candidate to {trait}), conflicts (evidence points away, e.g. a trait-matched QTL excludes the gene), neutral (context without a trait link).",
    "strength: strong = same-species experimental evidence or a trait-matched QTL plus GWAS convergence; moderate = trait-matched association, or an ortholog phenotype with medium/high-confidence orthology; weak = annotation-only, distance-only or a mention.",
    "At most one finding per gene and evidence type; do not restate the provisional score as a finding.",
    "Finish with specialist_done(summary): which genes you covered, the finding ids you recorded, and the gaps (domains not available in this build, or 'not found in <source>').",
)
SPECIALIST_STOP = (
    "Record each finding as soon as you have its evidence; do not save them for the end.",
    "Call specialist_done as soon as every focus gene is covered or tools stop yielding new evidence; do not spend the whole budget by default.",
    "You have at most {max_steps} tool rounds; the run ends your lane when they are used up.",
    "If a tool fails twice, stop calling it and report the gap.",
)
COMMON_TOOLS = ("get_evidence", "record_finding", "think", "specialist_done")
