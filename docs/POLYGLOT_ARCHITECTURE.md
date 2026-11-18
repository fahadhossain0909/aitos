# AITOS Polyglot Architecture

## Decision

AITOS remains Python-first. Python owns quantitative research, strategy policy, AI/ML/RL, regime intelligence, sentiment/news analysis and orchestration. Rust is introduced as an optional in-process native core for deterministic CPU-bound market-data primitives. The extension is not a required runtime dependency and is not wired into paper/live execution by this foundation change.

C++/CUDA and Go are deferred until profiling demonstrates a workload that cannot be served efficiently by Python/NumPy/Numba or Rust. TypeScript remains the operator-console/UI language already used by the desktop application.

## Responsibility boundaries

| Technology | Responsibility | Status |
|---|---|---|
| Python | Strategy, quant research, AI/ML/RL, learning, orchestration | Existing primary runtime |
| Rust | Fixed-point book primitives, future market-data/replay kernels | Optional foundation added |
| NumPy | Vectorized numerical computation | Existing dependency |
| Numba | JIT for supported numeric research kernels | Evaluate after baseline |
| Cython | Selective legacy acceleration | Deferred |
| C++/CUDA | Specialized SIMD/GPU/native integration | Deferred pending evidence |
| Go | Distributed control plane | Deferred; avoid duplicate services |
| TypeScript | Dashboard and desktop UI | Existing UI stack |

## Native boundary contract

- Use PyO3/Maturin for in-process calls; do not RPC each market event.
- Native inputs are integer price ticks and quantity lots. Decimal exchange strings must be converted using instrument filters/decimal arithmetic; binary floats must not cross this book boundary.
- A delta is applied atomically only after its payload and sequence contract are validated.
- Stale/duplicate updates are ignored. Sequence gaps fail closed and require authoritative snapshot resynchronization.
- Native failures must never be interpreted as an empty book or a trading signal.
- The Python reference implementation supports parity tests and installations without the optional wheel.

## Current scope

The aitos.native_core module exposes PythonFixedPointOrderBook, InstrumentScale, NativeOrderBookSession, create_order_book and native_available. InstrumentScale converts exchange decimal strings to integer ticks/lots with exact Decimal arithmetic and rejects off-tick/off-lot values. NativeOrderBookSession adapts the existing AITOS SymbolFilters, OrderBookSnapshot and DepthUpdate contracts into the native/reference core and returns decimal snapshots plus depth imbalance and microprice. The Rust crate provides snapshot initialization, first-delta bridging, linked subsequent updates, stale-event suppression, atomic level validation, top-N snapshots, depth imbalance and microprice. It remains opt-in and is deliberately not wired into the existing live LocalOrderBook/runtime path.

## Build and test

Python fallback: PYTHONPATH=. pytest -q native/python_tests/test_native_core.py

Rust tests: cargo test --manifest-path native/rust/Cargo.toml

Build extension: python -m pip install maturin; maturin develop --manifest-path native/rust/Cargo.toml; PYTHONPATH=. pytest -q native/python_tests/test_native_core.py

## Migration gates

1. Profiling identifies a measured CPU bottleneck.
2. A written contract defines units, sequence semantics, errors and ownership.
3. Python reference and Rust pass differential/property tests.
4. Recorded replay shows identical canonical outputs.
5. Fault injection proves gaps, malformed values, overflow and restarts fail closed.
6. Benchmarks record hardware, compiler, workload, warm-up, sample count and percentiles.
7. Shadow deployment is opt-in and read-only; no order path switches automatically.
8. Independent review and explicit approval precede production wiring.

## Rollback

The extension is optional. Uninstall the wheel or call create_order_book(..., prefer_native=False) to use the Python reference. No schema, exchange endpoint, risk threshold, strategy decision, Redis tuning or deployment workflow changes are included.

## Deferred work

Profile ingestion/order-flow hot paths; add golden Binance depth replay and differential parity against LocalOrderBook; evaluate Numba for numerical research kernels; add Arrow Flight/gRPC only for genuine cross-process boundaries; add Go/C++/CUDA only when measured evidence justifies them.
