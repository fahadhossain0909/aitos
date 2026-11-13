"""Deterministic, low-cost financial headline features.

This is a transparent baseline, not a fact verifier or a trained sentiment model.
Its outputs are annotations only; callers must not mark an event verified or
feed these scores directly into execution policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FinancialNLPResult:
    event_type: str
    sentiment: float
    severity: float
    confidence: float
    impact_horizon: str
    matched_terms: tuple[str, ...]
    analyzer_version: str = "financial-lexicon-v1"


# Phrase weights are intentionally conservative and inspectable.
_SENTIMENT_TERMS = {
    "beats estimates": 1.5,
    "raises guidance": 1.4,
    "record profit": 1.2,
    "strong demand": 0.9,
    "revenue growth": 0.8,
    "buyback": 0.6,
    "upgrade": 0.6,
    "partnership": 0.4,
    "approval": 0.7,
    "misses estimates": -1.5,
    "cuts guidance": -1.4,
    "sec probe": -1.3,
    "fraud": -1.5,
    "bankruptcy": -2.0,
    "default": -1.7,
    "hack": -1.3,
    "exploit": -1.2,
    "lawsuit": -0.8,
    "recall": -1.0,
    "layoffs": -0.5,
    "sanctions": -1.0,
    "war": -1.1,
    "tariff": -0.5,
    "rate hike": -0.4,
    "inflation": -0.3,
    "liquidation": -1.5,
    "outage": -0.8,
    "delay": -0.4,
    "selloff": -1.2,
    "plunge": -1.3,
    "হ্যাক": -1.3,
    "হ্যাকিং": -1.3,
    "তথ্য ফাঁস": -1.2,
    "সাইবার হামলা": -1.4,
    "দেউলিয়া": -1.8,
    "ঋণখেলাপি": -1.6,
    "যুদ্ধ": -1.1,
    "নিষেধাজ্ঞা": -0.9,
    "মুদ্রাস্ফীতি": -0.4,
    "সুদের হার বৃদ্ধি": -0.5,
    "সুদের হার কমানো": 0.4,
    "প্রতারণা": -1.5,
    "লোকসান": -1.0,
    "লাভ বৃদ্ধি": 1.0,
    "অনুমোদন": 0.7,
    "黑客攻击": -1.4,
    "数据泄露": -1.2,
    "破产": -1.8,
    "违约": -1.6,
    "战争": -1.1,
    "制裁": -0.9,
    "通货膨胀": -0.4,
    "加息": -0.5,
    "降息": 0.4,
    "欺诈": -1.5,
    "利润增长": 1.0,
    "ハッキング": -1.4,
    "情報漏えい": -1.2,
    "破産": -1.8,
    "債務不履行": -1.6,
    "戦争": -1.1,
    "制裁": -0.9,
    "インフレ": -0.4,
    "利上げ": -0.5,
    "利下げ": 0.4,
    "詐欺": -1.5,
    "利益増加": 1.0,
}

_EVENT_PATTERNS = (
    (
        "security",
        (
            "hack", "exploit", "breach", "stolen funds", "cyberattack",
            "হ্যাক", "হ্যাকিং", "তথ্য ফাঁস", "সাইবার হামলা",
            "黑客攻击", "数据泄露", "ハッキング", "情報漏えい",
        ),
    ),
    (
        "regulatory",
        ("sec probe", "lawsuit", "investigation", "sanctions", "fine", "নিষেধাজ্ঞা", "制裁"),
    ),
    (
        "earnings",
        ("earnings", "revenue", "profit", "guidance", "estimates", "লাভ বৃদ্ধি", "লোকসান", "利润增长", "利益増加"),
    ),
    (
        "macro",
        (
            "inflation", "interest rate", "rate hike", "rate cut", "central bank",
            "মুদ্রাস্ফীতি", "সুদের হার বৃদ্ধি", "সুদের হার কমানো",
            "通货膨胀", "加息", "降息", "インフレ", "利上げ", "利下げ",
        ),
    ),
    (
        "geopolitical",
        ("war", "tariff", "ceasefire", "election", "geopolitical", "যুদ্ধ", "战争", "戦争"),
    ),
    (
        "corporate_action",
        ("merger", "acquisition", "buyback", "dividend", "split", "অধিগ্রহণ", "একীভূতকরণ", "買収", "合併"),
    ),
    (
        "operations",
        ("outage", "recall", "production halt", "supply disruption", "উৎপাদন বন্ধ", "停产", "生産停止"),
    ),
    (
        "market_structure",
        ("liquidation", "short squeeze", "selloff", "plunge", "লিকুইডেশন", "清算"),
    ),
)
_HIGH_SEVERITY = {
    "bankruptcy",
    "default",
    "fraud",
    "hack",
    "exploit",
    "stolen funds",
    "war",
    "liquidation",
    "production halt",
    "হ্যাক",
    "হ্যাকিং",
    "দেউলিয়া",
    "ঋণখেলাপি",
    "যুদ্ধ",
    "প্রতারণা",
    "黑客攻击",
    "破产",
    "违约",
    "战争",
    "ハッキング",
    "破産",
    "債務不履行",
    "戦争",
}
_NEGATIONS = {
    "not",
    "no",
    "never",
    "without",
    "denies",
    "denied",
    "rejects",
    "rejected",
    "না",
    "নয়",
    "নয়",
    "অস্বীকার",
    "没有",
    "否认",
    "ない",
    "否定",
}


def _matches(text: str, phrase: str) -> list[re.Match]:
    pattern = re.compile(
        r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)",
        re.IGNORECASE,
    )
    return list(pattern.finditer(text))


def _is_negated(text: str, start: int) -> bool:
    preceding = re.findall(r"\w+", text[max(0, start - 48) : start].casefold())
    return any(token in _NEGATIONS for token in preceding[-3:])


def _has_unnegated_match(text: str, phrase: str) -> bool:
    return any(not _is_negated(text, match.start()) for match in _matches(text, phrase))


def analyze_financial_headline(text: str) -> FinancialNLPResult:
    """Return bounded headline annotations; confidence is capped at 0.55."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("headline text is required")
    if len(text) > 20_000:
        raise ValueError("headline text exceeds 20000 characters")

    normalized = text.casefold()
    weighted_signals: list[tuple[str, float]] = []
    for phrase, weight in _SENTIMENT_TERMS.items():
        for match in _matches(normalized, phrase):
            if not _is_negated(normalized, match.start()):
                weighted_signals.append((phrase, weight))

    total_weight = sum(weight for _, weight in weighted_signals)
    magnitude = sum(abs(weight) for _, weight in weighted_signals)
    sentiment = total_weight / magnitude if magnitude else 0.0
    sentiment = max(-1.0, min(1.0, sentiment))

    event_type = "general_news"
    for candidate, phrases in _EVENT_PATTERNS:
        if any(_has_unnegated_match(normalized, phrase) for phrase in phrases):
            event_type = candidate
            break

    matched = tuple(sorted({phrase for phrase, _ in weighted_signals}))
    high_severity = any(
        _has_unnegated_match(normalized, phrase) for phrase in _HIGH_SEVERITY
    )
    if high_severity:
        severity = 0.9
    elif event_type in {"security", "regulatory", "geopolitical"}:
        severity = 0.65
    elif event_type in {"earnings", "macro", "corporate_action"}:
        severity = 0.45
    elif matched:
        severity = 0.3
    else:
        severity = 0.1

    confidence = min(0.55, 0.1 + 0.12 * len(matched))
    if not matched:
        confidence = 0.05
    horizon = {
        "security": "immediate",
        "operations": "immediate",
        "earnings": "intraday",
        "market_structure": "intraday",
        "macro": "multi_day",
        "geopolitical": "multi_day",
        "regulatory": "multi_day",
        "corporate_action": "multi_day",
    }.get(event_type, "unknown")
    return FinancialNLPResult(
        event_type=event_type,
        sentiment=sentiment,
        severity=severity,
        confidence=confidence,
        impact_horizon=horizon,
        matched_terms=matched,
    )


__all__ = ["FinancialNLPResult", "analyze_financial_headline"]
