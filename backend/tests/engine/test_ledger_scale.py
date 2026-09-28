"""The chunked material ledger answers exactly like a brute-force recomputation."""

import random

from monxuplan_engine.materials import EPS, LedgerEvent, MaterialAccount


def _brute(events, t):
    evs = sorted(events)
    lvl_at = 0.0
    for e in evs:
        if e.time <= t:
            lvl_at += e.delta
    best = lvl_at
    lvl = 0.0
    for e in evs:
        lvl += e.delta
        if e.time > t and lvl < best:
            best = lvl
    return lvl_at, best


def test_chunked_ledger_matches_bruteforce():
    rng = random.Random(3)
    class Small(MaterialAccount):
        CHUNK = 8  # force many chunk splits

    acc = Small(0)
    evs = []
    for k in range(700):
        t = rng.randint(0, 5000)
        d = rng.choice([1, -1]) * rng.uniform(0.5, 40)
        e = LedgerEvent(t, d, "SUPPLY" if d > 0 else "CONSUMPTION", f"r{k}")
        acc.add(e)
        evs.append(e)
        if k % 50 == 0:
            for q in (0, rng.randint(0, 5000), 5000):
                lvl, avail = _brute(evs, q)
                assert abs(acc.level_at(q) - lvl) < 1e-6
                assert abs(acc.available_from(q) - avail) < 1e-6
    # earliest = first candidate instant where the brute-force availability covers the quantity
    times = sorted({0} | {e.time for e in evs})
    for qty in (1, 10, 50, 200):
        for t0 in (0, 1000, 2500):
            expect = next((t for t in times if t >= t0 and _brute(evs, t)[1] + EPS >= qty), None)
            if expect is not None and expect < t0:
                expect = t0
            got = acc.earliest(qty, t0)
            if _brute(evs, t0)[1] + EPS >= qty:
                assert got == t0
            else:
                assert got == expect
    assert [e.time for e in acc.events] == sorted(e.time for e in acc.events)
    acc.remove_ref("r5")
    assert all(e.ref != "r5" for e in acc.events) and len(acc.events) == 699
