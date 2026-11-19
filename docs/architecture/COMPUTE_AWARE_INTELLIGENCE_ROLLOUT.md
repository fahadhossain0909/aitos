# Compute-Aware Intelligence and Retrieval Upgrade

The shadow adapter observes task intents and reports proposed dispatch order. It
never delays, cancels, or replaces the caller's existing execution path. It is
not a durable broker or production executor. Raw exchange events and L3 deltas
must never be submitted as replaceable tasks.

The hybrid quantitative-memory ranker reranks already-retrieved graph/vector
candidates using lexical overlap, optional externally computed semantic score,
recency decay, and realized outcome evidence. Scores and component breakdowns
are provenance-tagged context metadata only, not trading signals, calibrated
probabilities, or execution authority. The helper has no embedding dependency
and does not create a new vector database.

Rollout gates:
1. Run shadow observation in paper while existing execution remains authoritative.
2. Compare observed intents with proposed dispatch and queue metrics.
3. Test position-monitor starvation under bursts and restart.
4. Evaluate retrieval relevance with labeled queries and time-split validation;
   exclude outcomes that were not known at decision time.
5. Keep position safety independently scheduled; do not enable candidate
   admission control until independent review and shadow evidence pass.
