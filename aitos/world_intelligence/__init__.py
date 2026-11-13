"""AITOS World Intelligence: advisory external-event context."""

from aitos.world_intelligence.contracts import (
    AssetImpact,
    IntelligenceEvent,
    SourceEvidence,
)
from aitos.world_intelligence.governor import IntelligenceRiskGovernor, RiskAssessment
from aitos.world_intelligence.impact import estimate_asset_impact
from aitos.world_intelligence.storage import ClickHouseIntelligenceStore
from aitos.world_intelligence.rss import RSSFeedAdapter
from aitos.world_intelligence.adapters import ingest_page, ingest_page_durably
from aitos.world_intelligence.snapshot import (
    HorizonImpact,
    AssetIntelligenceSnapshot,
    build_asset_intelligence_snapshot,
)
from aitos.world_intelligence.event_study import (
    EventStudyObservation,
    EventBetaEstimate,
    estimate_event_beta,
)
from aitos.world_intelligence.verification import (
    VerificationResult,
    verify_observations,
)
from aitos.world_intelligence.entity_resolution import (
    ResolvedEntity,
    EntityAliasRegistry,
    DEFAULT_ENTITY_REGISTRY,
)
from aitos.world_intelligence.narratives import NarrativeCluster, cluster_narratives
from aitos.world_intelligence.pipeline import EnrichmentResult, enrich_event_batch
from aitos.world_intelligence.providers import (
    GDELTDocAdapter,
    SECEdgarSubmissionsAdapter,
    FREDSeriesAdapter,
)
from aitos.world_intelligence.evaluation import (
    EventPrediction,
    EvaluationMetrics,
    WalkForwardSplit,
    AblationComparison,
    evaluate_predictions,
    chronological_walk_forward_splits,
    compare_ablation,
)
from aitos.world_intelligence.context import (
    AssetSnapshotProvider,
    ClickHouseAssetSnapshotProvider,
    snapshot_to_dict,
)
from aitos.world_intelligence.eventbus_adapter import EventBusSink
from aitos.world_intelligence.worker import (
    SourceRunResult,
    IntelligenceIngestionWorker,
)
from aitos.world_intelligence.financial_nlp import (
    FinancialNLPResult,
    analyze_financial_headline,
)

__all__ = [
    "AssetImpact",
    "IntelligenceEvent",
    "SourceEvidence",
    "IntelligenceRiskGovernor",
    "RiskAssessment",
    "estimate_asset_impact",
    "ClickHouseIntelligenceStore",
    "RSSFeedAdapter",
    "ingest_page",
    "ingest_page_durably",
    "FinancialNLPResult",
    "analyze_financial_headline",
    "VerificationResult",
    "verify_observations",
    "EventStudyObservation",
    "EventBetaEstimate",
    "estimate_event_beta",
    "HorizonImpact",
    "AssetIntelligenceSnapshot",
    "build_asset_intelligence_snapshot",
    "ResolvedEntity",
    "EntityAliasRegistry",
    "DEFAULT_ENTITY_REGISTRY",
    "NarrativeCluster",
    "cluster_narratives",
    "EnrichmentResult",
    "enrich_event_batch",
    "GDELTDocAdapter",
    "SECEdgarSubmissionsAdapter",
    "FREDSeriesAdapter",
    "EventPrediction",
    "EvaluationMetrics",
    "WalkForwardSplit",
    "AblationComparison",
    "evaluate_predictions",
    "chronological_walk_forward_splits",
    "compare_ablation",
    "AssetSnapshotProvider",
    "ClickHouseAssetSnapshotProvider",
    "snapshot_to_dict",
    "EventBusSink",
    "SourceRunResult",
    "IntelligenceIngestionWorker",
]
