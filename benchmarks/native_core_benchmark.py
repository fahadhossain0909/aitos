"""Reproducible local microbenchmark; not an end-to-end latency claim."""
import argparse
import statistics
import time
from aitos.native_core import PythonFixedPointOrderBook, create_order_book, native_available


def run(factory, events, repeats):
    samples=[]
    for _ in range(repeats):
        book=factory()
        start=time.perf_counter()
        for i in range(events):
            update=i+11
            book.apply_delta(update,update,bids=[(100000+i%100,1+i%7)],asks=[(100200+i%100,1+i%5)],previous_update_id=update-1 if i else None)
        samples.append(time.perf_counter()-start)
    return samples


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--events",type=int,default=100000)
    parser.add_argument("--repeats",type=int,default=5)
    args=parser.parse_args()
    if args.events<=0 or args.repeats<=0: parser.error("events and repeats must be positive")
    factories={"python-reference":lambda:PythonFixedPointOrderBook(10,[(99900,1)],[(100100,1)])}
    if native_available(): factories["rust-native"]=lambda:create_order_book(10,[(99900,1)],[(100100,1)])
    print(f"events={args.events} repeats={args.repeats} native_available={native_available()}")
    for name,factory in factories.items():
        samples=run(factory,args.events,args.repeats); median=statistics.median(samples)
        print(f"{name}: median={median:.6f}s events_per_second={args.events/median:.0f} min={min(samples):.6f}s max={max(samples):.6f}s")


if __name__=="__main__": main()
