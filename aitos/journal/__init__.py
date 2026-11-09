"""Journal package public API with lazy imports.

Importing a narrow persistence adapter must not eagerly import every policy,
analytics and review subsystem. Public names remain backward-compatible and
are loaded only when requested.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "ActivePolicy": (".policy_registry", "ActivePolicy"),
    "AdaptivePolicyEngine": (".adaptive_policy", "AdaptivePolicyEngine"),
    "DailyReview": (".models", "DailyReview"),
    "DecisionEvidenceAttributor": (
        ".evidence_attribution",
        "DecisionEvidenceAttributor",
    ),
    "DecisionPerformanceEvaluator": (
        ".performance_evaluator",
        "DecisionPerformanceEvaluator",
    ),
    "EvidencePerformance": (".evidence_attribution", "EvidencePerformance"),
    "EvidenceWeightCandidate": (
        ".evidence_weight_optimizer",
        "EvidenceWeightCandidate",
    ),
    "EvidenceWeightOptimizer": (
        ".evidence_weight_optimizer",
        "EvidenceWeightOptimizer",
    ),
    "JournalEntry": (".models", "JournalEntry"),
    "JournalEntryType": (".models", "JournalEntryType"),
    "JournalRepository": (".repository", "JournalRepository"),
    "JournalSystem": (".journal_system", "JournalSystem"),
    "MonthlyReview": (".models", "MonthlyReview"),
    "PerformanceReport": (".performance_evaluator", "PerformanceReport"),
    "PerformanceSlice": (".performance_evaluator", "PerformanceSlice"),
    "PolicyCandidate": (".adaptive_policy", "PolicyCandidate"),
    "PolicyGovernance": (".policy_governance", "PolicyGovernance"),
    "PolicyRegistry": (".policy_registry", "PolicyRegistry"),
    "PolicyVersion": (".policy_governance", "PolicyVersion"),
    "RegimePolicy": (".adaptive_policy", "RegimePolicy"),
    "ShadowPolicyResult": (".shadow_policy", "ShadowPolicyResult"),
    "ShadowWeightResult": (".evidence_shadow", "ShadowWeightResult"),
    "WeeklyReview": (".models", "WeeklyReview"),
    "daily_review": (".reviews", "daily_review"),
    "evaluate_shadow": (".shadow_policy", "evaluate_shadow"),
    "evaluate_weight_candidate": (
        ".evidence_shadow",
        "evaluate_weight_candidate",
    ),
    "monthly_review": (".reviews", "monthly_review"),
    "r_multiple": (".reviews", "r_multiple"),
    "weekly_review": (".reviews", "weekly_review"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
