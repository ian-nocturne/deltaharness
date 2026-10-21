"""PRISM Memory v8.0 — Modular Local-First Bitemporal Memory & Self-Healing Skill Engine."""
from .contracts import (
    VERSION,
    EVIDENCE_STATES,
    VERIFICATION_MODES,
    MemoryScope,
    ActorContext,
    StalenessVerdict,
    TrajectoryAttributionResult,
)
from .code_anchors import (
    cached_file_sha,
    normalized_symbol_ast_hash,
    extract_file_symbol_signatures,
    extract_file_symbol_hashes,
    is_breaking_symbol_change,
)
from .heuristics import analyze_trajectory_sentiment
from .indexing import (
    SparseCountsProxy,
    invalidate_task_caches,
    get_cached_task_rows,
    build_or_get_lexical_index,
    score_lexical_postings,
    score_decor_facets_postings,
    check_link_sync_needed,
    record_link_sync_state,
    build_shared_edges_inverted,
)
from .evaluation import (
    compute_skill_rates,
    verify_live_provenance_and_drift,
    evaluate_temporal_guard,
)
from .standalone import StandaloneStore, MemoryEngine
from .paths import (
    resolve_default_data_root,
    resolve_embedding_model_dir,
    check_model_status,
    doctor_environment,
    backup_data_root,
    restore_data_root,
)
from .repo_benchmark import (
    count_real_tokens,
    run_repo_evolution_benchmark,
)
from .audit import (
    audit_repository,
    format_audit_markdown,
    format_audit_sarif,
)
from .viberescue import (
    build_rescue_bundle,
    format_rescue_markdown,
    format_one_prompt_rescue,
    mask_sensitive_text,
)
from .anti_slop import (
    scan_anti_slop,
    format_anti_slop_markdown,
    format_anti_slop_patch_prompt,
    lock_design_system,
)

__all__ = [
    "VERSION",
    "EVIDENCE_STATES",
    "VERIFICATION_MODES",
    "MemoryScope",
    "ActorContext",
    "StalenessVerdict",
    "TrajectoryAttributionResult",
    "cached_file_sha",
    "normalized_symbol_ast_hash",
    "extract_file_symbol_signatures",
    "extract_file_symbol_hashes",
    "is_breaking_symbol_change",
    "analyze_trajectory_sentiment",
    "SparseCountsProxy",
    "invalidate_task_caches",
    "get_cached_task_rows",
    "build_or_get_lexical_index",
    "score_lexical_postings",
    "score_decor_facets_postings",
    "check_link_sync_needed",
    "record_link_sync_state",
    "build_shared_edges_inverted",
    "compute_skill_rates",
    "verify_live_provenance_and_drift",
    "evaluate_temporal_guard",
    "StandaloneStore",
    "MemoryEngine",
    "resolve_default_data_root",
    "resolve_embedding_model_dir",
    "check_model_status",
    "doctor_environment",
    "backup_data_root",
    "restore_data_root",
    "count_real_tokens",
    "run_repo_evolution_benchmark",
    "audit_repository",
    "format_audit_markdown",
    "format_audit_sarif",
    "build_rescue_bundle",
    "format_rescue_markdown",
    "format_one_prompt_rescue",
    "mask_sensitive_text",
    "scan_anti_slop",
    "format_anti_slop_markdown",
    "format_anti_slop_patch_prompt",
    "lock_design_system",
]


