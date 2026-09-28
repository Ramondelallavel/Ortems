from monxuplan_engine.contract import DurationSpec, SetupMatrixSpec, SetupRuleSpec
from monxuplan_engine.durations import run_minutes
from monxuplan_engine.materials import MaterialLedger, fifo_pegging
from monxuplan_engine.setups import SetupModel, state_key
from monxuplan_engine.timelines import CumulativeTimeline


def test_tiered_run_time():
    d = DurationSpec(setup_minutes=20, run_minutes_per_unit=3.4, run_tiers=[{"min_quantity": 1001, "minutes_per_unit": 3.1}])
    assert run_minutes(d, 1000) == 3400
    assert run_minutes(d, 1001) == 3104  # 1001 x 3.1 = 3103.1 -> rounded up
    assert run_minutes(d, 100, efficiency=0.8) == 425


def test_batch_run_time():
    d = DurationSpec(batch_size=50, minutes_per_batch=90)
    assert run_minutes(d, 120) == 270  # 3 oven cycles


def test_setup_matrix_and_rules():
    m = SetupMatrixSpec.model_validate(
        {
            "id": "COLOR",
            "attribute": "color",
            "same_minutes": 5,
            "default_minutes": 45,
            "entries": [{"from": "A", "to": "B", "minutes": 20}, {"from": "B", "to": "A", "minutes": 30}],
        }
    )
    rule = SetupRuleSpec(id="CLEAN", when_prev={"family": "Y"}, when_next={"family": "X"}, add_minutes=15)
    sm = SetupModel([m], [rule])
    sm.register_resource(0, "R0", ["COLOR"], "MAX")
    sm.register_resource(1, "R1", [], "MAX")
    a = state_key({"color": "A", "family": "X"})
    b = state_key({"color": "B", "family": "X"})
    c = state_key({"color": "C", "family": "Y"})
    assert sm.setup(0, a, a, 99) == 5
    assert sm.setup(0, a, b, 99) == 20
    assert sm.setup(0, b, a, 99) == 30
    assert sm.setup(0, a, c, 99) == 45
    assert sm.setup(0, None, a, 99) == 45  # unknown previous state
    assert sm.setup(0, c, a, 99) == 45 + 15  # cleaning rule Y -> X
    assert sm.setup(1, a, b, 12) == 12  # no matrix: routing setup


def test_ledger_never_creates_stock():
    led = MaterialLedger(1)
    led.supply(0, 0, 100, "OH")
    assert led.earliest(0, 100, 0) == 0
    assert led.earliest(0, 300, 0) is None
    led.supply(0, 500, 200, "PO1")
    assert led.earliest(0, 300, 0) == 500
    led.consume(0, 600, 250, "op1")
    # 50 left from t=600; a consumption at t=0 may use at most 50 (protects op1)
    assert led.earliest(0, 50, 0) == 0
    assert led.earliest(0, 60, 0) is None


def test_fifo_pegging_links_supply_to_consumers():
    led = MaterialLedger(1)
    led.supply(0, 0, 100, "OH")
    led.supply(0, 100, 100, "PO")
    led.consume(0, 10, 80, "a")
    led.consume(0, 150, 100, "b")
    links = [(s.ref, c.ref, q) for s, c, q in fifo_pegging(led.accounts[0])]
    assert links == [("OH", "a", 80), ("OH", "b", 20), ("PO", "b", 80)]


def test_cumulative_timeline_next_fit_and_reserve():
    tl = CumulativeTimeline(0, [(0, 2), (100, 1), (200, 2)])
    assert tl.next_fit(0, 50, 2) is None
    assert tl.next_fit(50, 150, 2) == 200
    tl.reserve(0, 300, 1)
    assert tl.next_fit(0, 50, 1) is None
    assert tl.next_fit(120, 180, 1) == 200
    tl.reserve(200, 300, 2)
    assert tl.shortfalls() == [(200, 300, 1)]
