"""Multi-premise dispatch under transformer limits, checked on cases small enough to
solve by hand."""

from __future__ import annotations

import numpy as np
import pytest

from der_opt.devices import BatteryStorage
from der_opt.network import (
    CapacityDeferral,
    Feeder,
    NetworkDispatchModel,
    NetworkElement,
    element_flows,
    loading_summary,
    solve_independently,
)
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing import ConstantPriceSeries
from der_opt.pricing.sources import PriceSeries
from der_opt.time_grid import TimeGrid

TG = TimeGrid(resolution_minutes=60, horizon_steps=4)


class ArrayPrice(PriceSeries):
    def __init__(self, values):
        self._values = np.asarray(values, dtype=float)

    def values(self, time_grid):
        return self._values


def _house(name, load, price=(0.1, 0.1, 0.3, 0.3), battery=True, **battery_kwargs):
    devices = []
    if battery:
        params = {"capacity_kwh": 10, "p_max_charge_kw": 5, "p_max_discharge_kw": 5,
                  "eta_charge": 1.0, "eta_discharge": 1.0, "soc_init_frac": 0.0} | battery_kwargs
        devices.append(BatteryStorage("battery", TG, **params))
    stream = EnergyArbitrage(ArrayPrice(price), ConstantPriceSeries(0.0), TG)
    return Premise(name, TG, devices=devices, value_streams=[stream], external_load=load, poi_limit_kw=20)


def _one_transformer(rating, overload_cost=None, premises=("h1",)):
    return Feeder("f", [NetworkElement("tx", rating_kw=rating, overload_cost_per_kwh=overload_cost)],
                  attachments={p: "tx" for p in premises})


def test_rating_limits_charging_and_prices_congestion():
    # Alone, the battery would charge 5 kW in the cheap hours (flow 7) to cover the
    # evening; the 4 kW rating lets it charge 2 kW, so the evening imports 3 kW at $0.30
    house = _house("h1", [2, 2, 5, 5])
    network = NetworkDispatchModel(_one_transformer(4), [house])
    result, model, backend = network.solve()
    assert result.flows["tx"].max() == pytest.approx(4)
    assert result.objective_value == pytest.approx(0.1 * 8 + 0.3 * 6, abs=1e-6)
    prices, exact = network.congestion_prices(model, backend)
    assert exact
    # one more kW through the transformer in a cheap hour displaces a kWh of charging,
    # which then has to be bought at $0.30 instead of $0.10
    # (to within the earliest-charging tie-break, $0.00001/kWh per hour)
    assert prices["tx"].iloc[0] == pytest.approx(0.2, abs=1e-3)
    assert prices["tx"].iloc[3] == pytest.approx(0.0, abs=1e-3)


def test_congestion_price_matches_the_cost_of_more_load():
    def cost(extra):
        load = [2 + extra, 2, 5, 5]
        return NetworkDispatchModel(_one_transformer(4), [_house("h1", load)]).solve()[0].objective_value

    eps = 0.01
    marginal = (cost(eps) - cost(0)) / eps
    assert marginal == pytest.approx(0.1 + 0.2, abs=1e-3)  # energy + congestion


def test_soft_rating_charges_for_overload():
    house = _house("h1", [6, 6, 1, 1], battery=False)
    result, _, _ = NetworkDispatchModel(_one_transformer(4, overload_cost=1.0), [house]).solve()
    assert result.network_costs["overload"] == pytest.approx(4.0)
    assert result.objective_value == pytest.approx(0.1 * 12 + 0.3 * 2 + 4.0)


def test_premises_with_identically_named_devices_share_one_model():
    houses = [_house("h1", [2, 2, 5, 5]), _house("h2", [1, 1, 1, 1])]
    network = NetworkDispatchModel(_one_transformer(6, premises=("h1", "h2")), houses)
    result, _, _ = network.solve()
    assert set(result.premises) == {"h1", "h2"}
    assert result.flows["tx"].max() <= 6 + 1e-6
    assert sum(r.objective_value for r in result.premises.values()) == pytest.approx(result.objective_value)


def test_flows_add_up_the_tree_and_independent_solves_ignore_it():
    feeder = Feeder(
        "f",
        [NetworkElement("lateral", rating_kw=100), NetworkElement("tx_a", rating_kw=4, parent="lateral"),
         NetworkElement("tx_b", parent="lateral")],
        attachments={"h1": "tx_a", "h2": "tx_b"},
    )
    houses = [_house("h1", [2, 2, 5, 5]), _house("h2", [1, 1, 1, 1], battery=False)]
    result, _, _ = NetworkDispatchModel(feeder, houses).solve()
    np.testing.assert_allclose(result.flows["lateral"], result.flows["tx_a"] + result.flows["tx_b"])

    alone = element_flows(feeder, solve_independently([_house("h1", [2, 2, 5, 5]), _house("h2", [1, 1, 1, 1], battery=False)]))
    assert alone["tx_a"].max() == pytest.approx(7)  # charges at full power, blind to the 4 kW rating
    summary = loading_summary(alone, feeder, TG)
    assert summary.loc["tx_a", "hours_over"] == 2
    assert summary.loc["tx_a", "kwh_over"] == pytest.approx(6)


def test_capacity_deferral_flattens_the_peak():
    # flat energy price, so only the deferral value moves the battery
    house = _house("h1", [1, 1, 5, 1], price=(0.1,) * 4, soc_init_frac=0.0)
    feeder = _one_transformer(None)
    result, _, _ = NetworkDispatchModel(feeder, [house], [CapacityDeferral("tx", value_per_kw=1.0)]).solve()
    # charging x kWh in each of the first two hours: peak = max(1 + x, 5 - 2x) -> x = 4/3
    assert result.flows["tx"].max() == pytest.approx(7 / 3)
    assert result.network_costs["tx capacity deferral"] == pytest.approx(7 / 3)


def test_feeder_validation():
    with pytest.raises(ValueError, match="parent"):
        Feeder("f", [NetworkElement("tx", parent="nowhere")])
    with pytest.raises(ValueError, match="cycle"):
        Feeder("f", [NetworkElement("a", parent="b"), NetworkElement("b", parent="a")])
    with pytest.raises(ValueError, match="unknown element"):
        Feeder("f", [NetworkElement("tx")], attachments={"h1": "elsewhere"})
    with pytest.raises(ValueError, match="not attached"):
        NetworkDispatchModel(Feeder("f", [NetworkElement("tx")]), [_house("h1", [1, 1, 1, 1])])


def test_ties_between_equally_cheap_hours_go_to_the_earliest():
    # flat price: charging any time is equally cheap; the house charges first thing
    house = _house("h1", [1, 1, 1, 1], price=(0.1,) * 4, soc_init_frac=0.0, soc_final_min_frac=0.4)
    for dispatch in (
        solve_independently([house])["h1"],
        NetworkDispatchModel(_one_transformer(None), [_house("h1", [1, 1, 1, 1], price=(0.1,) * 4, soc_init_frac=0.0, soc_final_min_frac=0.4)]).solve()[0].premises["h1"],
    ):
        # a lossless battery moves every import as early as it can: 5 kW of charging
        # in hour 1, the last 1 kW in hour 2, then serves the load from storage
        assert dispatch.grid_import.tolist() == pytest.approx([6, 2, 0, 0])
        assert dispatch.objective_value == pytest.approx(0.1 * 8)  # tie-break not reported
