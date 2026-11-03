"""Counterfactual explanations — spec §33.2: "What would change the
decision?" Computed directly from the Opportunity Scanner's weighted
component scores, no model required — this is arithmetic on numbers we
already have, not a learned counterfactual generator.
"""

from __future__ import annotations


def composite_score(
    component_scores: dict[str, float], weights: dict[str, float]
) -> float:
    return (
        sum(
            component_scores.get(k, 0.0) * weights.get(k, 0.0) for k in component_scores
        )
        * 10
    )


def counterfactual_for_threshold(
    component_scores: dict[str, float], weights: dict[str, float], threshold: float
) -> list[str]:
    """Explain what single-dimension change would flip a pass/fail decision
    against ``threshold`` (e.g. the scanner's ``min_score_threshold``).

    If the candidate currently clears the threshold, explains what would
    have to *worsen* to fail it. If it currently fails, explains what
    single dimension improving would be enough to clear it (when that's
    achievable within the dimension's 0-10 range).
    """
    current = composite_score(component_scores, weights)
    messages: list[str] = []

    if current >= threshold:
        for dim, score in component_scores.items():
            weight = weights.get(dim, 0.0)
            contribution = score * weight * 10
            hypothetical = current - contribution
            if hypothetical < threshold and contribution > 0:
                messages.append(
                    f"If {dim.replace('_', ' ')} had scored 0 instead of {score:.1f}/10, "
                    f"the composite score would drop to {hypothetical:.1f}, below the {threshold:.1f} threshold."
                )
    else:
        gap = threshold - current
        for dim, score in component_scores.items():
            weight = weights.get(dim, 0.0)
            if weight <= 0:
                continue
            max_possible_gain = (10.0 - score) * weight * 10
            if max_possible_gain >= gap:
                needed_score = min(10.0, score + gap / (weight * 10))
                messages.append(
                    f"If {dim.replace('_', ' ')} improved from {score:.1f}/10 to {needed_score:.1f}/10, "
                    f"the composite score would reach the {threshold:.1f} threshold."
                )

    return messages
