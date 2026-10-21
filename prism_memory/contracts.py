"""Typed data contracts and governance invariants for PRISM Memory v8.1."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

VERSION = "8.1.0"

EVIDENCE_STATES = (
    "reported",
    "trial-used",
    "compatible-unverified",
    "feedback-verified",
    "user-confirmed",
    "needs_revision",
)

VERIFICATION_MODES = (
    "derived",
    "implicit-trajectory",
    "explicit-feedback",
    "explicit-helpful-feedback",
    "execution-proof",
    "subprocess-execution",
    "explicit-user",
    "ast-self-healed",
    "markdown-coedit",
    "unverified-disk-sync",
)


@dataclass(frozen=True)
class MemoryScope:
    """Identifies the workspace, project, task, and git branch for a memory operation."""
    task_id: str
    project_id: str = "default"
    root_path: str = ""
    git_branch: str = "main"
    shared: bool = False
    federated: bool = False


@dataclass(frozen=True)
class ActorContext:
    """Identifies the caller session, authorization mode, and explicit user intent."""
    session_id: str
    explicit_user: bool = False
    verified_execution: bool = False
    writable: bool = True


@dataclass
class StalenessVerdict:
    """Structured result of AST signature and base-level activation staleness evaluation."""
    stale: bool
    reasons: List[str] = field(default_factory=list)
    referenced_file_count: int = 0
    changed_files: List[str] = field(default_factory=list)
    changed_symbols: List[str] = field(default_factory=list)
    symbol_anchored_saved: List[str] = field(default_factory=list)
    breaking_changes: List[str] = field(default_factory=list)
    compatible_changes: List[str] = field(default_factory=list)
    unanchored_changes: List[str] = field(default_factory=list)
    self_healable: bool = False
    verification_status: str = "verified"
    self_healed_count: int = 0
    inactive_days: float = 0.0
    effective_stale_days: float = 30.0
    actr_activation: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stale": self.stale,
            "reasons": list(self.reasons),
            "referencedFileCount": self.referenced_file_count,
            "changedFiles": list(self.changed_files),
            "changedSymbols": list(self.changed_symbols),
            "symbolAnchoredSaved": list(self.symbol_anchored_saved),
            "breakingChanges": list(self.breaking_changes),
            "compatibleChanges": list(self.compatible_changes),
            "unanchoredChanges": list(self.unanchored_changes),
            "selfHealable": self.self_healable,
            "verificationStatus": self.verification_status,
            "selfHealedCount": self.self_healed_count,
            "inactiveDays": round(self.inactive_days, 2),
            "effectiveStaleDays": round(self.effective_stale_days, 1),
            "actrActivation": self.actr_activation,
        }


@dataclass
class TrajectoryAttributionResult:
    """Result of negation-aware turn trajectory sentiment and credit assignment."""
    attributed: bool
    sentiment: str
    credited_skills: List[Dict[str, Any]] = field(default_factory=list)
    credited_items: List[str] = field(default_factory=list)
    trajectory_warning: Optional[Dict[str, Any]] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        res = {
            "attributed": self.attributed,
            "sentiment": self.sentiment,
            "creditedSkills": list(self.credited_skills),
            "creditedItems": list(self.credited_items),
            "trajectoryWarning": self.trajectory_warning,
        }
        if self.reason:
            res["reason"] = self.reason
        return res
