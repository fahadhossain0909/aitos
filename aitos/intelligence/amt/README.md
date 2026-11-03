# Production AMT Engine

This package is the structured Auction Market Theory layer for AITOS.

## Current capabilities

- Executed-trade volume profile (price bins)
- POC
- 70% configurable value area (VAH/VAL)
- Basic HVN/LVN extraction
- Initial Balance from a configurable UTC session start
- Acceptance/rejection context
- POC-based value migration
- Auction state classification
- Basic day-type classification
- Optional L2 depth confirmation

## Data integrity rule

Do not call candle volume a volume profile. `build_volume_profile()` requires
executed trades, so the resulting POC/VAH/VAL are explicitly trade-volume based.

## Next integration step

Replace the scanner's scalar `auction_score` dependency with `AMTContext`, while
retaining the existing `auction.py` and `live_auction.py` functions as backward-
compatible fallback adapters during migration.
