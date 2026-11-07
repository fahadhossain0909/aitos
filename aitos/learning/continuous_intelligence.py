"""Read-only performance intelligence for AITOS.

This module turns the existing append-only Experience Store into auditable
trade attribution, evidence-backed diagnostics, counterfactual replay summaries
and drift alerts. It never changes a strategy, risk limit, model or order.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from math import isclose, isfinite, log, sqrt
from statistics import mean, pstdev
from typing import Any


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("metadata") or {}
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class TradeObservation:
    experience_id: str
    source: str
    symbol: str
    side: str
    strategy: str
    regime: str
    realized_r: float
    mae_r: float | None = None
    mfe_r: float | None = None
    cost_r: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> TradeObservation:
        metadata = _metadata(record)
        if record.get("outcome") != "closed":
            raise ValueError("only closed-trade experiences are eligible")
        reward = _number(record.get("reward"))
        if reward is None:
            raise ValueError("experience reward must be finite")
        market_state = record.get("market_state") or {}
        risk_state = record.get("risk_state") or {}
        features = record.get("features") or {}
        market_state = dict(market_state) if isinstance(market_state, Mapping) else {}
        risk_state = dict(risk_state) if isinstance(risk_state, Mapping) else {}
        features = dict(features) if isinstance(features, Mapping) else {}
        source = str(record.get("source") or "unknown").strip().lower()
        decision = str(record.get("decision") or "").strip().lower()
        if source not in {"backtest", "paper", "live"}:
            raise ValueError("experience source is not eligible for attribution")
        if source == "backtest" and decision != "backtest_trade":
            raise ValueError("only realized backtest trade outcomes are eligible")
        unit = str(metadata.get("reward_unit") or "").strip().lower()
        if unit in {"r", "r_multiple", "risk_multiple"}:
            reward_r = reward
        elif unit in {"", "quote_pnl", "quote_currency", "usd"}:
            if source in {"paper", "live"} and not unit:
                raise ValueError("legacy paper/live quote PnL is not eligible")
            raw_risk = (
                risk_state.get("risk_amount_usd")
                or risk_state.get("initial_risk_usd")
                or features.get("risk_amount_usd")
                or features.get("initial_risk_usd")
            )
            risk_amount = _number(raw_risk)
            if risk_amount is None or risk_amount <= 0:
                raise ValueError("quote PnL requires positive initial risk")
            reward_r = reward / risk_amount
        else:
            raise ValueError("experience reward is not safely R-normalized")
        symbol = str(record.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("experience symbol is required")
        mae = _number(metadata.get("mae_r"))
        mfe = _number(metadata.get("mfe_r"))
        cost = _number(metadata.get("total_cost_r"))
        side_value = (
            metadata.get("side")
            or features.get("side")
            or record.get("side")
            or record.get("decision")
            or "unknown"
        )
        if str(side_value).lower() in {"backtest_trade", "backtest_summary"}:
            side_value = "unknown"
        return cls(
            experience_id=str(
                record.get("experience_id")
                or metadata.get("trade_id")
                or metadata.get("decision_id")
                or ""
            ),
            source=source,
            symbol=symbol,
            side=str(side_value).upper(),
            strategy=str(
                metadata.get("strategy_id")
                or record.get("strategy_version")
                or "unknown"
            ),
            regime=str(
                metadata.get("regime") or market_state.get("regime") or "unknown"
            ).lower(),
            realized_r=reward_r,
            mae_r=mae,
            mfe_r=mfe,
            cost_r=cost,
            metadata=metadata,
        )


@dataclass(frozen=True)
class PerformanceSlice:
    key: str
    samples: int
    win_rate: float
    mean_realized_r: float
    mean_mae_r: float | None
    mean_mfe_r: float | None
    mean_cost_r: float | None
    profit_factor: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RootCauseFinding:
    category: str
    samples: int
    evidence_ids: tuple[str, ...]
    confidence: float
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_ids"] = list(self.evidence_ids)
        return value


@dataclass(frozen=True)
class ResearchHypothesis:
    category: str
    statement: str
    experiment: str
    sample_count: int
    evidence_ids: tuple[str, ...]
    approval_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_ids"] = list(self.evidence_ids)
        return value


@dataclass(frozen=True)
class CounterfactualResult:
    scenario_id: str
    evidence_id: str
    actual_realized_r: float
    alternative_gross_r: float
    estimated_cost_r: float
    alternative_net_r: float
    delta_vs_actual_r: float
    replay_source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DriftFeatureResult:
    feature: str
    baseline_samples: int
    current_samples: int
    population_stability_index: float
    status: str


@dataclass(frozen=True)
class DriftReport:
    status: str
    minimum_samples: int
    features: tuple[DriftFeatureResult, ...]
    reward_mean_z_score: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "minimum_samples": self.minimum_samples,
            "features": [asdict(item) for item in self.features],
            "reward_mean_z_score": self.reward_mean_z_score,
        }


@dataclass(frozen=True)
class ContinuousIntelligenceReport:
    eligible_trades: int
    rejected_experiences: int
    performance_by_strategy_regime: tuple[PerformanceSlice, ...]
    performance_by_symbol_side: tuple[PerformanceSlice, ...]
    root_causes: tuple[RootCauseFinding, ...]
    counterfactuals: tuple[CounterfactualResult, ...]
    drift: DriftReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible_trades": self.eligible_trades,
            "rejected_experiences": self.rejected_experiences,
            "performance_by_strategy_regime": [
                item.to_dict() for item in self.performance_by_strategy_regime
            ],
            "performance_by_symbol_side": [
                item.to_dict() for item in self.performance_by_symbol_side
            ],
            "root_causes": [item.to_dict() for item in self.root_causes],
            "counterfactuals": [item.to_dict() for item in self.counterfactuals],
            "drift": self.drift.to_dict() if self.drift else None,
        }


class DriftDetector:
    """Small-sample-aware PSI and reward-shift monitor; alerts only, never adapts."""

    def __init__(
        self,
        *,
        minimum_samples: int = 30,
        psi_threshold: float = 0.20,
        reward_z_threshold: float = 2.0,
        epsilon: float = 1e-6,
    ) -> None:
        if minimum_samples < 2 or psi_threshold <= 0 or reward_z_threshold <= 0:
            raise ValueError("invalid drift detector thresholds")
        if not 0 < epsilon < 0.1:
            raise ValueError("epsilon must be between 0 and 0.1")
        self.minimum_samples = int(minimum_samples)
        self.psi_threshold = float(psi_threshold)
        self.reward_z_threshold = float(reward_z_threshold)
        self.epsilon = float(epsilon)

    @staticmethod
    def _finite(values: Iterable[Any]) -> list[float]:
        return [value for item in values if (value := _number(item)) is not None]

    def _psi(self, baseline: Sequence[float], current: Sequence[float]) -> float:
        ordered = sorted(baseline)
        # Quantile-derived boundaries adapt to the baseline, not the current data.
        boundaries = [
            ordered[min(len(ordered) - 1, int(len(ordered) * q / 10))]
            for q in range(1, 10)
        ]

        def counts(values: Sequence[float]) -> list[int]:
            result = [0] * 10
            for value in values:
                index = 0
                while index < len(boundaries) and value > boundaries[index]:
                    index += 1
                result[index] += 1
            return result

        base_counts, current_counts = counts(baseline), counts(current)
        base_total, current_total = len(baseline), len(current)
        score = 0.0
        for left, right in zip(base_counts, current_counts):
            p = max(self.epsilon, left / base_total)
            q = max(self.epsilon, right / current_total)
            score += (q - p) * log(q / p)
        return float(score)

    def evaluate(
        self,
        baseline_features: Mapping[str, Iterable[Any]],
        current_features: Mapping[str, Iterable[Any]],
        *,
        baseline_rewards: Iterable[Any] = (),
        current_rewards: Iterable[Any] = (),
    ) -> DriftReport:
        feature_results: list[DriftFeatureResult] = []
        insufficient = False
        detected = False
        for feature in sorted(set(baseline_features) & set(current_features)):
            baseline = self._finite(baseline_features[feature])
            current = self._finite(current_features[feature])
            if (
                len(baseline) < self.minimum_samples
                or len(current) < self.minimum_samples
            ):
                insufficient = True
                status, psi = "insufficient_samples", 0.0
            else:
                psi = self._psi(baseline, current)
                status = "drift" if psi >= self.psi_threshold else "stable"
                detected = detected or status == "drift"
            feature_results.append(
                DriftFeatureResult(feature, len(baseline), len(current), psi, status)
            )

        base_reward = self._finite(baseline_rewards)
        current_reward = self._finite(current_rewards)
        reward_z: float | None = None
        if base_reward or current_reward:
            if (
                len(base_reward) < self.minimum_samples
                or len(current_reward) < self.minimum_samples
            ):
                insufficient = True
            else:
                baseline_deviation = pstdev(base_reward)
                current_deviation = pstdev(current_reward)
                standard_error = sqrt(
                    baseline_deviation**2 / len(base_reward)
                    + current_deviation**2 / len(current_reward)
                )
                if standard_error > 0:
                    reward_z = (
                        abs(mean(current_reward) - mean(base_reward)) / standard_error
                    )
                    detected = detected or reward_z >= self.reward_z_threshold
                elif mean(current_reward) != mean(base_reward):
                    # A constant baseline has zero variance; any measured shift
                    # is a drift signal, not an infinite/invalid z-score.
                    detected = True
        if not feature_results and not base_reward and not current_reward:
            insufficient = True
        status = (
            "drift_detected"
            if detected
            else "insufficient_samples" if insufficient else "stable"
        )
        return DriftReport(
            status, self.minimum_samples, tuple(feature_results), reward_z
        )


class ContinuousIntelligenceEngine:
    """Read-only analyzer over canonical Experience Store records."""

    ROOT_CAUSE_LABELS = {
        "data_quality": "Market data was stale, missing, invalid or incomplete.",
        "entry_quality": (
            "The recorded entry-quality diagnostic identified an entry defect."
        ),
        "regime_mismatch": (
            "The strategy/regime diagnostic identified a regime mismatch."
        ),
        "adverse_order_flow": (
            "Recorded order-flow evidence was adverse to the position."
        ),
        "execution_cost": "Recorded execution costs materially affected the outcome.",
        "premature_exit": "The exit diagnostic explicitly marked a premature exit.",
        "missed_opportunity": (
            "The exit diagnostic recorded missed opportunity evidence."
        ),
        "risk_control": "A risk-control event was explicitly attached to the outcome.",
    }

    def __init__(
        self,
        drift_detector: DriftDetector | None = None,
        *,
        material_cost_r_threshold: float = 0.25,
    ) -> None:
        if material_cost_r_threshold <= 0 or not isfinite(material_cost_r_threshold):
            raise ValueError("material cost threshold must be finite and positive")
        self.drift_detector = drift_detector or DriftDetector()
        self.material_cost_r_threshold = float(material_cost_r_threshold)

    @staticmethod
    def _slice(key: str, rows: Sequence[TradeObservation]) -> PerformanceSlice:
        rewards = [row.realized_r for row in rows]
        wins = [value for value in rewards if value > 0]
        losses = [value for value in rewards if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))

        def collect(name: str) -> list[float]:
            values = []
            for row in rows:
                value = getattr(row, name)
                if value is not None:
                    values.append(value)
            return values

        mae, mfe, costs = (
            collect("mae_r"),
            collect("mfe_r"),
            collect("cost_r"),
        )
        return PerformanceSlice(
            key=key,
            samples=len(rows),
            win_rate=len(wins) / len(rows),
            mean_realized_r=mean(rewards),
            mean_mae_r=mean(mae) if mae else None,
            mean_mfe_r=mean(mfe) if mfe else None,
            mean_cost_r=mean(costs) if costs else None,
            profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
        )

    def _root_causes(
        self, rows: Sequence[TradeObservation]
    ) -> tuple[RootCauseFinding, ...]:
        grouped: dict[str, list[TradeObservation]] = defaultdict(list)
        for row in rows:
            metadata = row.metadata
            decision_snapshot = metadata.get("decision_snapshot") or {}
            if not isinstance(decision_snapshot, Mapping):
                decision_snapshot = {}
            diagnostics = metadata.get("diagnostics") or {}
            if not isinstance(diagnostics, Mapping):
                diagnostics = {}
            tags = (
                metadata.get("root_cause_tags")
                or metadata.get("failure_tags")
                or diagnostics.get("root_cause_tags")
                or diagnostics.get("failure_tags")
                or decision_snapshot.get("root_cause_tags")
                or ()
            )
            if isinstance(tags, str):
                tags = (tags,)
            explicit = {str(tag).strip().lower() for tag in tags}
            explicit.update(
                label
                for key, label in (
                    ("data_stale", "data_quality"),
                    ("entry_quality_failed", "entry_quality"),
                    ("regime_mismatch", "regime_mismatch"),
                    ("adverse_order_flow", "adverse_order_flow"),
                    ("execution_degraded", "execution_cost"),
                    ("premature_exit", "premature_exit"),
                    ("missed_opportunity", "missed_opportunity"),
                    ("risk_control_triggered", "risk_control"),
                )
                if (
                    metadata.get(key) is True
                    or diagnostics.get(key) is True
                    or decision_snapshot.get(key) is True
                )
            )
            missed_r = _number(metadata.get("missed_opportunity_r"))
            if missed_r is not None and missed_r > 0:
                explicit.add("missed_opportunity")
            total_cost_r = metadata.get("total_cost_r", diagnostics.get("total_cost_r"))
            if total_cost_r is not None:
                cost = _number(total_cost_r)
                if cost is not None and cost >= self.material_cost_r_threshold:
                    explicit.add("execution_cost")
            for category in explicit:
                if category in self.ROOT_CAUSE_LABELS:
                    grouped[category].append(row)
        findings = []
        for category, evidence in sorted(grouped.items()):
            ids = tuple(row.experience_id for row in evidence if row.experience_id)
            # This is evidence coverage, not a calibrated probability of causation.
            coverage = len(evidence) / max(1, len(rows))
            findings.append(
                RootCauseFinding(
                    category=category,
                    samples=len(evidence),
                    evidence_ids=ids[:100],
                    confidence=min(1.0, coverage),
                    explanation=self.ROOT_CAUSE_LABELS[category],
                )
            )
        return tuple(findings)

    @staticmethod
    def _counterfactuals(
        scenarios: Iterable[Mapping[str, Any]],
        actual_outcomes: Mapping[str, float],
    ) -> tuple[CounterfactualResult, ...]:
        results = []
        seen_scenarios: set[str] = set()
        for item in scenarios:
            if item.get("source_type") != "historical_replay":
                raise ValueError("counterfactuals require historical_replay evidence")
            evidence_id = str(item.get("evidence_id") or "").strip()
            scenario_id = str(item.get("scenario_id") or "").strip()
            replay_source = str(item.get("replay_source") or "").strip()
            if not evidence_id or not scenario_id or not replay_source:
                raise ValueError(
                    "counterfactual replay requires evidence and provenance"
                )
            if evidence_id not in actual_outcomes:
                raise ValueError("counterfactual evidence_id is not a report trade")
            if scenario_id in seen_scenarios:
                raise ValueError("counterfactual scenario_id must be unique")
            seen_scenarios.add(scenario_id)
            actual = _number(item.get("actual_realized_r"))
            gross = _number(item.get("alternative_gross_r"))
            costs = _number(item.get("estimated_cost_r"))
            if actual is None or gross is None or costs is None or costs < 0:
                raise ValueError(
                    "counterfactual R values must be finite and costs non-negative"
                )
            if not isclose(
                actual, actual_outcomes[evidence_id], rel_tol=1e-6, abs_tol=1e-6
            ):
                raise ValueError(
                    "counterfactual actual_realized_r does not match stored outcome"
                )
            net = gross - costs
            results.append(
                CounterfactualResult(
                    scenario_id,
                    evidence_id,
                    actual,
                    gross,
                    costs,
                    net,
                    net - actual,
                    replay_source,
                )
            )
        return tuple(results)

    @staticmethod
    def generate_hypotheses(
        report: ContinuousIntelligenceReport,
        *,
        minimum_samples: int = 5,
    ) -> tuple[ResearchHypothesis, ...]:
        """Turn supported diagnostics into research questions, never live changes."""
        if minimum_samples < 1:
            raise ValueError("minimum hypothesis sample count must be positive")
        templates = {
            "data_quality": (
                "Determine whether stale or incomplete market data contributed to "
                "the affected decisions.",
                "Replay stale/missing-data failure cases and verify that freshness "
                "gates fail closed without changing strategy or risk limits.",
            ),
            "entry_quality": (
                "Determine which recorded entry-quality component contributed to "
                "the affected outcomes.",
                "Run one-factor entry-component ablations on the same point-in-time "
                "dataset, then evaluate walk-forward and locked holdout results.",
            ),
            "regime_mismatch": (
                "Determine whether strategy performance degrades in the recorded "
                "market regime.",
                "Compare strategy-by-regime variants using identical costs, "
                "walk-forward windows and a locked holdout.",
            ),
            "adverse_order_flow": (
                "Determine whether adverse order-flow evidence should change entry "
                "eligibility or confidence.",
                "Replay a single OFI/CVD confirmation ablation by regime while "
                "preserving all capital and risk hard gates.",
            ),
            "execution_cost": (
                "Determine whether execution friction materially erodes the "
                "strategy's expected edge.",
                "Compare passive/aggressive execution and liquidity-aware cost "
                "assumptions on the same historical fills and market path.",
            ),
            "premature_exit": (
                "Determine whether Exit Intelligence closes positions before the "
                "entry thesis is invalidated.",
                "Replay the same entries with one isolated exit-policy variant; "
                "compare net R, MAE, drawdown and holding time out of sample.",
            ),
            "missed_opportunity": (
                "Determine whether target checkpoints or path planning leave "
                "repeatable opportunity uncaptured.",
                "Compare checkpoint-only and evidence-approved target extensions "
                "with identical execution costs and no lookahead.",
            ),
            "risk_control": (
                "Determine whether recorded risk-control triggers are correctly "
                "classified and consistently enforced.",
                "Audit trigger evidence and false-positive/negative cases without "
                "relaxing any hard capital-protection constraint.",
            ),
        }
        hypotheses = []
        for finding in report.root_causes:
            template = templates.get(finding.category)
            if (
                template is None
                or finding.samples < minimum_samples
                or not finding.evidence_ids
            ):
                continue
            hypotheses.append(
                ResearchHypothesis(
                    category=finding.category,
                    statement=template[0],
                    experiment=template[1],
                    sample_count=finding.samples,
                    evidence_ids=finding.evidence_ids,
                )
            )
        return tuple(sorted(hypotheses, key=lambda item: item.category))

    def analyze_store(
        self,
        store: Any,
        start: Any,
        end: Any,
        *,
        sources: Iterable[str] | None = None,
        limit: int = 100_000,
        baseline_features: Mapping[str, Iterable[Any]] | None = None,
        current_features: Mapping[str, Iterable[Any]] | None = None,
        baseline_rewards: Iterable[Any] = (),
        current_rewards: Iterable[Any] = (),
        counterfactual_scenarios: Iterable[Mapping[str, Any]] = (),
    ) -> ContinuousIntelligenceReport:
        """Analyze a bounded ClickHouseExperienceStore window without hot-path work."""
        query = getattr(store, "query_closed_experiences", None)
        if not callable(query):
            raise TypeError("store must provide query_closed_experiences()")
        records = query(start, end, sources=sources, limit=limit)
        return self.analyze(
            records,
            baseline_features=baseline_features,
            current_features=current_features,
            baseline_rewards=baseline_rewards,
            current_rewards=current_rewards,
            counterfactual_scenarios=counterfactual_scenarios,
        )

    def analyze(
        self,
        records: Iterable[Mapping[str, Any] | Any],
        *,
        baseline_features: Mapping[str, Iterable[Any]] | None = None,
        current_features: Mapping[str, Iterable[Any]] | None = None,
        baseline_rewards: Iterable[Any] = (),
        current_rewards: Iterable[Any] = (),
        counterfactual_scenarios: Iterable[Mapping[str, Any]] = (),
    ) -> ContinuousIntelligenceReport:
        eligible: list[TradeObservation] = []
        rejected = 0
        for item in records:
            record = item.as_dict() if hasattr(item, "as_dict") else item
            if not isinstance(record, Mapping):
                rejected += 1
                continue
            try:
                eligible.append(TradeObservation.from_record(record))
            except (TypeError, ValueError):
                rejected += 1

        by_strategy: dict[str, list[TradeObservation]] = defaultdict(list)
        by_symbol: dict[str, list[TradeObservation]] = defaultdict(list)
        for row in eligible:
            by_strategy[f"{row.strategy}|{row.regime}"].append(row)
            by_symbol[f"{row.symbol}|{row.side}"].append(row)

        baseline_reward_values = tuple(baseline_rewards)
        current_reward_values = tuple(current_rewards)
        drift = None
        has_feature_windows = (
            baseline_features is not None and current_features is not None
        )
        has_reward_windows = bool(baseline_reward_values) or bool(current_reward_values)
        if has_feature_windows or has_reward_windows:
            drift = self.drift_detector.evaluate(
                baseline_features or {},
                current_features or {},
                baseline_rewards=baseline_reward_values,
                current_rewards=current_reward_values,
            )
        return ContinuousIntelligenceReport(
            eligible_trades=len(eligible),
            rejected_experiences=rejected,
            performance_by_strategy_regime=tuple(
                self._slice(key, rows) for key, rows in sorted(by_strategy.items())
            ),
            performance_by_symbol_side=tuple(
                self._slice(key, rows) for key, rows in sorted(by_symbol.items())
            ),
            root_causes=self._root_causes(eligible),
            counterfactuals=self._counterfactuals(
                counterfactual_scenarios,
                {
                    row.experience_id: row.realized_r
                    for row in eligible
                    if row.experience_id
                },
            ),
            drift=drift,
        )
