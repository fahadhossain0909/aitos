use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::BTreeMap;

// Prices are integer ticks and quantities are integer lots at this boundary.
type PriceQuantity = (i64, i64);
type DepthDelta = (
    u64,
    u64,
    Vec<PriceQuantity>,
    Vec<PriceQuantity>,
    Option<u64>,
);
type BookSnapshot = (u64, Vec<PriceQuantity>, Vec<PriceQuantity>);

#[derive(Clone)]
struct Book {
    last: u64,
    bids: BTreeMap<i64, i64>,
    asks: BTreeMap<i64, i64>,
    first_delta: bool,
}

impl Book {
    fn validate_levels(rows: &[(i64, i64)]) -> PyResult<()> {
        for (price, quantity) in rows {
            if *price <= 0 {
                return Err(PyValueError::new_err("price units must be positive"));
            }
            if *quantity < 0 {
                return Err(PyValueError::new_err("quantity units cannot be negative"));
            }
        }
        Ok(())
    }

    fn new(last: u64, bids: Vec<(i64, i64)>, asks: Vec<(i64, i64)>) -> PyResult<Self> {
        Self::validate_levels(&bids)?;
        Self::validate_levels(&asks)?;
        let mut book = Self {
            last,
            bids: BTreeMap::new(),
            asks: BTreeMap::new(),
            first_delta: true,
        };
        for (price, quantity) in bids {
            if quantity > 0 {
                book.bids.insert(price, quantity);
            }
        }
        for (price, quantity) in asks {
            if quantity > 0 {
                book.asks.insert(price, quantity);
            }
        }
        Ok(book)
    }

    fn apply(
        &mut self,
        first: u64,
        final_id: u64,
        previous: Option<u64>,
        bids: Vec<(i64, i64)>,
        asks: Vec<(i64, i64)>,
    ) -> PyResult<bool> {
        if first > final_id {
            return Err(PyValueError::new_err(
                "first_update_id exceeds final_update_id",
            ));
        }
        Self::validate_levels(&bids)?;
        Self::validate_levels(&asks)?;
        if final_id <= self.last {
            return Ok(false);
        }
        if self.first_delta {
            let bridge = self
                .last
                .checked_add(1)
                .ok_or_else(|| PyValueError::new_err("snapshot update id overflow"))?;
            if !(first <= bridge && bridge <= final_id) {
                return Err(PyValueError::new_err(
                    "first delta does not bridge snapshot",
                ));
            }
        } else if previous != Some(self.last) {
            return Err(PyValueError::new_err("order-book sequence gap"));
        }

        for (price, quantity) in bids {
            if quantity == 0 {
                self.bids.remove(&price);
            } else {
                self.bids.insert(price, quantity);
            }
        }
        for (price, quantity) in asks {
            if quantity == 0 {
                self.asks.remove(&price);
            } else {
                self.asks.insert(price, quantity);
            }
        }
        self.last = final_id;
        self.first_delta = false;
        Ok(true)
    }

    fn depth_imbalance(&self, depth: usize) -> f64 {
        let bid_quantity: i128 = self
            .bids
            .values()
            .rev()
            .take(depth)
            .map(|quantity| i128::from(*quantity))
            .sum();
        let ask_quantity: i128 = self
            .asks
            .values()
            .take(depth)
            .map(|quantity| i128::from(*quantity))
            .sum();
        let total = bid_quantity + ask_quantity;
        if total == 0 {
            0.0
        } else {
            (bid_quantity - ask_quantity) as f64 / total as f64
        }
    }

    fn microprice_ticks(&self) -> Option<f64> {
        let (bid, bid_quantity) = self.bids.iter().next_back()?;
        let (ask, ask_quantity) = self.asks.iter().next()?;
        let total = i128::from(*bid_quantity) + i128::from(*ask_quantity);
        if total <= 0 {
            return None;
        }
        Some(
            (*ask as f64 * *bid_quantity as f64 + *bid as f64 * *ask_quantity as f64)
                / total as f64,
        )
    }
}

#[pyclass]
struct FixedPointOrderBook {
    book: Book,
}

#[pymethods]
impl FixedPointOrderBook {
    #[new]
    #[pyo3(signature = (snapshot_id, bids=Vec::new(), asks=Vec::new()))]
    fn new(snapshot_id: u64, bids: Vec<(i64, i64)>, asks: Vec<(i64, i64)>) -> PyResult<Self> {
        Ok(Self {
            book: Book::new(snapshot_id, bids, asks)?,
        })
    }

    #[pyo3(signature = (first_update_id, final_update_id, bids=Vec::new(), asks=Vec::new(), previous_update_id=None))]
    fn apply_delta(
        &mut self,
        first_update_id: u64,
        final_update_id: u64,
        bids: Vec<(i64, i64)>,
        asks: Vec<(i64, i64)>,
        previous_update_id: Option<u64>,
    ) -> PyResult<bool> {
        self.book.apply(
            first_update_id,
            final_update_id,
            previous_update_id,
            bids,
            asks,
        )
    }

    fn apply_deltas(&mut self, updates: Vec<DepthDelta>) -> PyResult<usize> {
        // A malformed later delta must not leave the live book partially
        // advanced. Commit the batch only after every sequence check succeeds.
        let mut candidate = self.book.clone();
        let mut applied = 0;
        for (first, final_id, bids, asks, previous) in updates {
            if candidate.apply(first, final_id, previous, bids, asks)? {
                applied += 1;
            }
        }
        self.book = candidate;
        Ok(applied)
    }

    fn snapshot(&self, depth: usize) -> PyResult<BookSnapshot> {
        if depth == 0 {
            return Err(PyValueError::new_err("depth must be positive"));
        }
        Ok((
            self.book.last,
            self.book
                .bids
                .iter()
                .rev()
                .take(depth)
                .map(|(price, quantity)| (*price, *quantity))
                .collect(),
            self.book
                .asks
                .iter()
                .take(depth)
                .map(|(price, quantity)| (*price, *quantity))
                .collect(),
        ))
    }

    fn best_bid_ask(&self) -> (Option<i64>, Option<i64>) {
        (
            self.book.bids.keys().next_back().copied(),
            self.book.asks.keys().next().copied(),
        )
    }

    fn depth_imbalance(&self, depth: usize) -> PyResult<f64> {
        if depth == 0 {
            return Err(PyValueError::new_err("depth must be positive"));
        }
        Ok(self.book.depth_imbalance(depth))
    }

    fn microprice_ticks(&self) -> Option<f64> {
        self.book.microprice_ticks()
    }
}

#[pymodule]
fn aitos_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<FixedPointOrderBook>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bridges_then_requires_linked_sequence() {
        let mut book = Book::new(10, vec![(100, 2)], vec![(102, 3)]).unwrap();
        assert!(book.apply(10, 11, None, vec![(101, 4)], vec![]).unwrap());
        assert!(book
            .apply(12, 12, Some(11), vec![], vec![(102, 0), (103, 5)])
            .unwrap());
        assert_eq!(book.bids.keys().next_back(), Some(&101));
        assert!(book.apply(13, 14, Some(12), vec![], vec![]).is_ok());
    }

    #[test]
    fn gaps_and_bad_levels_are_atomic() {
        let mut book = Book::new(10, vec![(100, 2)], vec![]).unwrap();
        assert!(book.apply(12, 13, None, vec![(101, 4)], vec![]).is_err());
        assert!(book.apply(11, 11, None, vec![(101, -1)], vec![]).is_err());
        assert_eq!(book.last, 10);
        assert_eq!(book.bids.len(), 1);
    }

    #[test]
    fn calculates_depth_imbalance_and_microprice() {
        let book = Book::new(10, vec![(100, 3), (99, 1)], vec![(102, 1), (103, 3)]).unwrap();
        assert_eq!(book.depth_imbalance(2), 0.0);
        assert_eq!(book.depth_imbalance(1), 0.5);
        assert_eq!(book.microprice_ticks(), Some(101.5));
    }
}
