use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::BTreeMap;

#[derive(Clone)]
struct Book {
    last: u64,
    bids: BTreeMap<i64, i64>,
    asks: BTreeMap<i64, i64>,
    first_delta: bool,
}
impl Book {
    fn levels(rows: &[(i64, i64)]) -> PyResult<()> {
        for (p, q) in rows {
            if *p <= 0 { return Err(PyValueError::new_err("price units must be positive")); }
            if *q < 0 { return Err(PyValueError::new_err("quantity units cannot be negative")); }
        }
        Ok(())
    }
    fn new(last: u64, bids: Vec<(i64, i64)>, asks: Vec<(i64, i64)>) -> PyResult<Self> {
        Self::levels(&bids)?; Self::levels(&asks)?;
        let mut book = Self { last, bids: BTreeMap::new(), asks: BTreeMap::new(), first_delta: true };
        for (p,q) in bids { if q > 0 { book.bids.insert(p,q); } }
        for (p,q) in asks { if q > 0 { book.asks.insert(p,q); } }
        Ok(book)
    }
    fn apply(&mut self, first: u64, final_id: u64, previous: Option<u64>, bids: Vec<(i64,i64)>, asks: Vec<(i64,i64)>) -> PyResult<bool> {
        if first > final_id { return Err(PyValueError::new_err("first_update_id exceeds final_update_id")); }
        Self::levels(&bids)?; Self::levels(&asks)?;
        if final_id <= self.last { return Ok(false); }
        if self.first_delta {
            let bridge = self.last.checked_add(1).ok_or_else(|| PyValueError::new_err("snapshot update id overflow"))?;
            if !(first <= bridge && bridge <= final_id) { return Err(PyValueError::new_err("first delta does not bridge snapshot")); }
        } else if previous != Some(self.last) {
            return Err(PyValueError::new_err("order-book sequence gap"));
        }
        for (p,q) in bids { if q == 0 { self.bids.remove(&p); } else { self.bids.insert(p,q); } }
        for (p,q) in asks { if q == 0 { self.asks.remove(&p); } else { self.asks.insert(p,q); } }
        self.last = final_id; self.first_delta = false; Ok(true)
    }
}
#[pyclass]
struct FixedPointOrderBook { book: Book }
#[pymethods]
impl FixedPointOrderBook {
    #[new]
    #[pyo3(signature=(snapshot_id, bids=Vec::new(), asks=Vec::new()))]
    fn new(snapshot_id:u64,bids:Vec<(i64,i64)>,asks:Vec<(i64,i64)>)->PyResult<Self> {
        Ok(Self { book: Book::new(snapshot_id,bids,asks)? })
    }
    #[pyo3(signature=(first_update_id,final_update_id,bids=Vec::new(),asks=Vec::new(),previous_update_id=None))]
    fn apply_delta(&mut self,first_update_id:u64,final_update_id:u64,bids:Vec<(i64,i64)>,asks:Vec<(i64,i64)>,previous_update_id:Option<u64>)->PyResult<bool> {
        self.book.apply(first_update_id,final_update_id,previous_update_id,bids,asks)
    }
    fn snapshot(&self,depth:usize)->PyResult<(u64,Vec<(i64,i64)>,Vec<(i64,i64)>)> {
        if depth==0 { return Err(PyValueError::new_err("depth must be positive")); }
        Ok((self.book.last,self.book.bids.iter().rev().take(depth).map(|(p,q)|(*p,*q)).collect(),self.book.asks.iter().take(depth).map(|(p,q)|(*p,*q)).collect()))
    }
    fn best_bid_ask(&self)->(Option<i64>,Option<i64>) {
        (self.book.bids.keys().next_back().copied(),self.book.asks.keys().next().copied())
    }
}
#[pymodule]
fn aitos_native(module:&Bound<'_,PyModule>)->PyResult<()> { module.add_class::<FixedPointOrderBook>()?; Ok(()) }

#[cfg(test)]
mod tests {
 use super::*;
 #[test] fn bridges_then_requires_linked_sequence() {
  let mut b=Book::new(10,vec![(100,2)],vec![(102,3)]).unwrap();
  assert!(b.apply(10,11,None,vec![(101,4)],vec![]).unwrap());
  assert!(b.apply(12,12,Some(11),vec![],vec![(102,0),(103,5)]).unwrap());
  assert_eq!(b.bids.keys().next_back(),Some(&101));
  assert!(b.apply(13,14,Some(12),vec![],vec![]).is_ok());
 }
 #[test] fn gaps_and_bad_levels_are_atomic() {
  let mut b=Book::new(10,vec![(100,2)],vec![]).unwrap();
  assert!(b.apply(12,13,None,vec![(101,4)],vec![]).is_err());
  assert!(b.apply(11,11,None,vec![(101,-1)],vec![]).is_err());
  assert_eq!(b.last,10); assert_eq!(b.bids.len(),1);
 }
}
