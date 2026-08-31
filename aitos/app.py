"""System wiring for the AITOS trading application."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

from aitos.core.contracts import AITOSModule, Event
from aitos.data.ingestion import DataIngestionService
from aitos.data.repository import MarketDataRepository
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.exchange.base import ExchangeAdapter
from aitos.execution.order_executor import OrderExecutor
from aitos.intelligence.rl_feedback import RLFeedbackLoop
from aitos.intelligence.rl_policy import RLPolicyScorer, TabularBanditRLScorer
from aitos.intelligence.scanner import OpportunityScanner
from aitos.journal.decision_repository import DecisionJournalRepository
from aitos.journal.journal_system import JournalSystem
from aitos.journal.performance_evaluator import DecisionPerformanceEvaluator
from aitos.journal.policy_monitor_service import PolicyMonitorService
from aitos.journal.repository import JournalRepository
from aitos.kernel.ai_kernel import AIKernel
from aitos.knowledge_graph.correlation_updater import SymbolCorrelationUpdater
from aitos.knowledge_graph.writer import GraphDriver, KnowledgeGraphWriter
from aitos.logging_setup import get_logger
from aitos.models.trade import TradeLifecycleState
from aitos.risk.models import PortfolioState, PositionExposure, RiskLimits
from aitos.risk.risk_engine import RiskEngine
from aitos.trading.lifecycle import TradeLifecycle
from aitos.trading.position_manager import PositionManager
from aitos.trading.reconciliation import ReconciliationScheduler
from aitos.xai.attention_explainer import AttentionExplainer
from aitos.xai.attention_feedback import AttentionFeedbackLoop
from aitos.xai.ml_explainer import TradeOutcomeClassifier
from aitos.xai.ml_feedback import MLExplainerFeedbackLoop

logger = get_logger("aitos.app")
