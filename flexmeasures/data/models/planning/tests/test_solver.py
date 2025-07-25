from __future__ import annotations

from datetime import datetime, timedelta
import pytest
import pytz
import logging

import numpy as np
import pandas as pd
from pandas.tseries.frequencies import to_offset
from sqlalchemy import select

from flexmeasures.data.models.time_series import Sensor
from flexmeasures.data.models.planning import Scheduler
from flexmeasures.data.models.planning.exceptions import InfeasibleProblemException
from flexmeasures.data.models.planning.storage import (
    StorageScheduler,
    add_storage_constraints,
    validate_storage_constraints,
    build_device_soc_values,
)
from flexmeasures.data.models.planning.linear_optimization import device_scheduler
from flexmeasures.data.models.planning.tests.utils import (
    check_constraints,
    get_sensors_from_db,
)
from flexmeasures.data.models.planning.utils import (
    initialize_device_commitment,
    initialize_df,
    initialize_energy_commitment,
    initialize_series,
)
from flexmeasures.data.schemas.sensors import TimedEventSchema
from flexmeasures.utils.calculations import (
    apply_stock_changes_and_losses,
    integrate_time_series,
)
from flexmeasures.tests.utils import get_test_sensor
from flexmeasures.utils.unit_utils import convert_units, ur

from pyomo.environ import value

TOLERANCE = 0.00001


@pytest.mark.parametrize(
    "initial_stock, stock_deltas, expected_stocks, storage_efficiency",
    [
        (
            1000,
            [100, -100, -100, 100],
            [1000, 1089, 979.11, 870.3189, 960.615711],
            0.99,
        ),
        (
            2.5,
            [-0.5, -0.5, -0.5, -0.5],
            [2.5, 1.8, 1.17, 0.603, 0.0927],
            0.9,
        ),
    ],
)
def test_storage_loss_function(
    initial_stock, stock_deltas, expected_stocks, storage_efficiency
):
    stocks = apply_stock_changes_and_losses(
        initial_stock,
        stock_deltas,
        storage_efficiency=storage_efficiency,
        how="left",
        decimal_precision=6,
    )
    print(stocks)
    assert all(a == b for a, b in zip(stocks, expected_stocks))


@pytest.mark.parametrize("use_inflexible_device", [False, True])
@pytest.mark.parametrize("battery_name", ["Test battery", "Test small battery"])
def test_battery_solver_day_1(
    add_battery_assets,
    add_inflexible_device_forecasts,
    use_inflexible_device,
    battery_name,
    db,
):
    epex_da, battery = get_sensors_from_db(
        db, add_battery_assets, battery_name=battery_name
    )
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 1))
    end = tz.localize(datetime(2015, 1, 2))
    resolution = timedelta(minutes=15)
    soc_at_start = battery.get_attribute("soc_in_mwh")
    scheduler: Scheduler = StorageScheduler(
        battery,
        start,
        end,
        resolution,
        flex_model={"soc-at-start": soc_at_start},
        flex_context={
            "inflexible-device-sensors": (
                [s.id for s in add_inflexible_device_forecasts.keys()]
                if use_inflexible_device
                else []
            ),
            "site-power-capacity": "2 MW",
        },
    )
    schedule = scheduler.compute()

    # Check if constraints were met
    check_constraints(battery, schedule, soc_at_start)


@pytest.mark.parametrize(
    "roundtrip_efficiency, storage_efficiency",
    [
        (1, 1),
        (1, 0.999),
        (1, 0.5),
        (0.99, 1),
        (0.01, 1),
    ],
)
def test_battery_solver_day_2(
    add_battery_assets, roundtrip_efficiency: float, storage_efficiency: float, db
):
    """Check battery scheduling results for day 2, which is set up with
    8 expensive, then 8 cheap, then again 8 expensive hours.
    If efficiency losses aren't too bad, we expect the scheduler to:
    - completely discharge within the first 8 hours
    - completely charge within the next 8 hours
    - completely discharge within the last 8 hours
    If efficiency losses are bad, the price difference is not worth cycling the battery,
    and so we expect the scheduler to only:
    - completely discharge within the last 8 hours
    """
    _epex_da, battery = get_sensors_from_db(db, add_battery_assets)
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 2))
    end = tz.localize(datetime(2015, 1, 3))
    resolution = timedelta(minutes=15)
    soc_at_start = battery.get_attribute("soc_in_mwh")
    soc_min = 0.5
    soc_max = 4.5
    scheduler = StorageScheduler(
        battery,
        start,
        end,
        resolution,
        flex_model={
            "soc-at-start": soc_at_start,
            "soc-min": soc_min,
            "soc-max": soc_max,
            "roundtrip-efficiency": roundtrip_efficiency,
            "storage-efficiency": storage_efficiency,
            "prefer-curtailing-later": False,
        },
    )
    schedule = scheduler.compute()

    # Check if constraints were met
    soc_schedule = check_constraints(
        battery, schedule, soc_at_start, roundtrip_efficiency, storage_efficiency
    )

    # Check whether the resulting soc schedule follows our expectations for 8 expensive, 8 cheap and 8 expensive hours
    assert soc_schedule.iloc[-1] == max(
        soc_min, battery.get_attribute("min_soc_in_mwh")
    )  # Battery sold out at the end of its planning horizon

    # As long as the efficiencies aren't too bad (I haven't computed the actual switch points)
    if roundtrip_efficiency > 0.9 and storage_efficiency > 0.9:
        np.testing.assert_approx_equal(
            soc_schedule.loc[start + timedelta(hours=8)],
            max(soc_min, battery.get_attribute("min_soc_in_mwh")),
            significant=3,
        )  # Sell what you begin with
        assert soc_schedule.loc[start + timedelta(hours=16)] == min(
            soc_max, battery.get_attribute("max_soc_in_mwh")
        )  # Buy what you can to sell later
    elif storage_efficiency > 0.9:
        # If only the roundtrip efficiency is poor, best to stand idle (keep a high SoC as long as possible)
        assert soc_schedule.loc[start + timedelta(hours=8)] == battery.get_attribute(
            "soc_in_mwh"
        )
        assert soc_schedule.loc[start + timedelta(hours=16)] == battery.get_attribute(
            "soc_in_mwh"
        )
    else:
        # If the storage efficiency is poor, regardless of whether the roundtrip efficiency is poor, best to sell asap
        assert soc_schedule.loc[start + timedelta(hours=8)] == max(
            soc_min, battery.get_attribute("min_soc_in_mwh")
        )
        assert soc_schedule.loc[start + timedelta(hours=16)] == max(
            soc_min, battery.get_attribute("min_soc_in_mwh")
        )


def run_test_charge_discharge_sign(
    battery,
    roundtrip_efficiency,
    consumption_price_sensor_id,
    production_price_sensor_id,
):
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 3))
    end = tz.localize(datetime(2015, 1, 4))
    resolution = timedelta(hours=1)
    storage_efficiency = 1
    # Choose the SoC constraints and starting value such that the battery can fully charge or discharge in a single time step
    soc_min = 0
    capacity = battery.get_attribute(
        "capacity_in_mw",
        ur.Quantity(battery.get_attribute("site-power-capacity")).to("MW").magnitude,
    )
    soc_max = capacity
    soc_at_start = capacity

    scheduler: Scheduler = StorageScheduler(
        battery,
        start,
        end,
        resolution,
        flex_model={
            "soc-min": soc_min,
            "soc-max": soc_max,
            "soc-at-start": soc_at_start,
            "roundtrip-efficiency": roundtrip_efficiency,
            "storage-efficiency": storage_efficiency,
            "prefer-charging-sooner": True,
            "prefer-curtailing-later": False,
        },
        flex_context={
            "consumption-price": {"sensor": consumption_price_sensor_id},
            "production-price": {"sensor": production_price_sensor_id},
        },
    )

    (
        sensors,
        start,
        end,
        resolution,
        soc_at_start,
        device_constraints,
        ems_constraints,
        commitments,
    ) = scheduler._prepare(skip_validation=True)

    _, _, results, model = device_scheduler(
        device_constraints=device_constraints,
        ems_constraints=ems_constraints,
        commitments=commitments,
        initial_stock=[
            soc_at_start_d * (timedelta(hours=1) / resolution)
            for soc_at_start_d in soc_at_start
        ],
    )

    device_power_sign = pd.Series(model.device_power_sign.extract_values())[0]
    device_power_up = pd.Series(model.device_power_up.extract_values())[0]
    device_power_down = pd.Series(model.device_power_down.extract_values())[0]

    is_power_down = ~np.isclose(abs(device_power_down), 0)
    is_power_up = ~np.isclose(abs(device_power_up), 0)

    # only one power active at a time
    assert (~(is_power_down & is_power_up)).all()

    # downwards power not active when the binary variable is 1
    assert (~is_power_down[device_power_sign == 1.0]).all()

    # upwards power not active when the binary variable is 0
    assert (~is_power_up[device_power_sign == 0.0]).all()

    schedule = initialize_series(
        data=[model.ems_power[0, j].value for j in model.j],
        start=start,
        end=end,
        resolution=to_offset(resolution),
    )

    # Check if constraints were met
    soc_schedule = check_constraints(
        battery, schedule, soc_at_start[0], roundtrip_efficiency, storage_efficiency
    )

    return schedule.tz_convert(tz), soc_schedule.tz_convert(tz)


def test_battery_solver_day_3(add_battery_assets, add_inflexible_device_forecasts, db):
    """Check battery scheduling results for day 3, which is set up with
    8 hours with negative prices, followed by 16 expensive hours.

    Under certain conditions, batteries can be used to "burn" energy in form of heat, due to the conversion
    losses of the inverters. Nonetheless, this doesn't come for free as this is shortening the lifetime of the asset.
    For this reason, the constraints `device_up_derivative_sign` and `device_down_derivative_sign' make sure that
    the storage can only charge or discharge within the same time period.

    These constraints don't avoid burning energy in Case 1) in which a storage with conversion losses operating under the
    same buy/sell prices.

    Nonetheless, as shown in Cases 3) and 4), the oscillatory dynamic is gone when having Consumption Price > Production Price.
    This is because even though the energy consumed is bigger than that produced, the difference between the cost of consuming and the
    revenue of producing doesn't create a profit.
    """

    roundtrip_efficiency = 0.9
    epex_da = get_test_sensor(db)
    epex_da_production = db.session.execute(
        select(Sensor).filter_by(name="epex_da_production")
    ).scalar_one_or_none()
    battery = add_battery_assets["Test battery"].sensors[0]

    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 3))

    # Case 1: Consumption Price = Production Price, roundtrip_efficiency < 1
    schedule_1, soc_schedule_1 = run_test_charge_discharge_sign(
        battery, roundtrip_efficiency, epex_da.id, epex_da.id
    )

    # For the negative price period, the schedule shows oscillations
    # discharge in even hours
    assert all(schedule_1[:8:2] < 0)  # 12am, 2am, 4am, 6am

    # charge in odd hours
    assert all(schedule_1[1:8:2] > 0)  # 1am, 3am, 5am, 7am

    # in positive price hours, the battery will only discharge to sell the energy charged in the negative hours
    assert all(schedule_1.loc[start + timedelta(hours=8) :] <= 0)

    # Case 2: Consumption Price = Production Price, roundtrip_efficiency = 1
    schedule_2, soc_schedule_2 = run_test_charge_discharge_sign(
        battery, 1, epex_da.id, epex_da.id
    )
    assert all(np.isclose(schedule_2[:8], 0))  # no oscillation

    # Case 3: Consumption Price > Production Price, roundtrip_efficiency < 1
    # In this case, we expect the battery to hold the energy that has initially and sell it during the period of
    # positive prices.
    schedule_3, soc_schedule_3 = run_test_charge_discharge_sign(
        battery, roundtrip_efficiency, epex_da.id, epex_da_production.id
    )
    assert all(np.isclose(schedule_3[:8], 0))  # no oscillation
    assert all(schedule_3[8:] <= 0)

    # discharge the whole battery in 1 time period
    capacity = battery.get_attribute(
        "capacity_in_mw",
        ur.Quantity(battery.get_attribute("site-power-capacity")).to("MW").magnitude,
    )
    assert np.isclose(
        schedule_3.min(),
        -capacity * np.sqrt(roundtrip_efficiency),
    )

    # Case 4: Consumption Price > Production Price, roundtrip_efficiency < 1
    schedule_4, soc_schedule_4 = run_test_charge_discharge_sign(
        battery, 1, epex_da.id, epex_da_production.id
    )

    assert all(np.isclose(schedule_4[:8], 0))  # no oscillation
    assert all(schedule_4[8:] <= 0)

    # discharge the whole battery in 1 time period, with no conversion losses
    assert np.isclose(schedule_4.min(), -capacity)


@pytest.mark.parametrize(
    "target_soc, charging_station_name",
    [
        (1, "Test charging station"),
        (5, "Test charging station"),
        (0, "Test charging station (bidirectional)"),
        (5, "Test charging station (bidirectional)"),
    ],
)
def test_charging_station_solver_day_2(
    target_soc, charging_station_name, setup_planning_test_data, db
):
    """Starting with a state of charge 1 kWh, within 2 hours we should be able to reach
    any state of charge in the range [1, 5] kWh for a unidirectional station,
    or [0, 5] for a bidirectional station, given a charging capacity of 2 kW.
    """
    soc_at_start = 1
    duration_until_target = timedelta(hours=2)

    epex_da = get_test_sensor(db)
    charging_station = setup_planning_test_data[charging_station_name].sensors[0]
    capacity = charging_station.get_attribute(
        "capacity_in_mw",
        ur.Quantity(charging_station.get_attribute("site-power-capacity"))
        .to("MW")
        .magnitude,
    )
    assert capacity == 2
    assert charging_station.get_attribute("consumption-price") == {"sensor": epex_da.id}
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 2))
    end = tz.localize(datetime(2015, 1, 3))
    resolution = timedelta(minutes=15)
    target_soc_datetime = start + duration_until_target
    soc_targets = initialize_series(np.nan, start, end, resolution, inclusive="right")
    soc_targets.loc[target_soc_datetime] = target_soc
    scheduler = StorageScheduler(
        charging_station,
        start,
        end,
        resolution,
        flex_model={
            "soc_at_start": soc_at_start,
            "soc_min": charging_station.get_attribute("min_soc_in_mwh", 0),
            "soc_max": charging_station.get_attribute(
                "max_soc_in_mwh", max(soc_targets.values)
            ),
            "roundtrip_efficiency": charging_station.get_attribute(
                "roundtrip_efficiency", 1
            ),
            "storage_efficiency": charging_station.get_attribute(
                "storage_efficiency", 1
            ),
            "soc_targets": soc_targets,
        },
        flex_context={
            "ems_power_capacity_in_mw": ur.Quantity("2 MVA"),
            "consumption_price": epex_da,
            "shared_currency_unit": "EUR",
        },
    )
    # soc targets are already a DataFrame, names get underscore, shared currency unit is set
    scheduler.config_deserialized = True

    consumption_schedule = scheduler.compute()
    soc_schedule = integrate_time_series(
        consumption_schedule, soc_at_start, decimal_precision=6
    )

    # Check if constraints were met
    assert min(consumption_schedule.values) >= capacity * -1
    assert max(consumption_schedule.values) <= capacity + TOLERANCE
    print(consumption_schedule.head(12))
    print(soc_schedule.head(12))
    assert abs(soc_schedule.loc[target_soc_datetime] - target_soc) < TOLERANCE


@pytest.mark.parametrize(
    "target_soc, charging_station_name",
    [
        (9, "Test charging station"),
        (15, "Test charging station"),
        (5, "Test charging station (bidirectional)"),
        (15, "Test charging station (bidirectional)"),
    ],
)
def test_fallback_to_unsolvable_problem(
    target_soc, charging_station_name, setup_planning_test_data, db
):
    """Starting with a state of charge 10 kWh, within 2 hours we should be able to reach
    any state of charge in the range [10, 14] kWh for a unidirectional station,
    or [6, 14] for a bidirectional station, given a charging capacity of 2 kW.
    Here we test target states of charge outside that range, ones that we should be able
    to get as close to as 1 kWh difference.
    We want our scheduler to handle unsolvable problems like these with a sensible fallback policy.

    The StorageScheduler raises an Exception which triggers the creation of a new job to compute a fallback
    schedule.
    """
    soc_at_start = 10
    duration_until_target = timedelta(hours=2)
    expected_gap = 1

    epex_da = get_test_sensor(db)
    charging_station = setup_planning_test_data[charging_station_name].sensors[0]
    capacity = charging_station.get_attribute(
        "capacity_in_mw",
        ur.Quantity(charging_station.get_attribute("site-power-capacity"))
        .to("MW")
        .magnitude,
    )
    assert capacity == 2
    assert charging_station.get_attribute("consumption-price") == {"sensor": epex_da.id}
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 2))
    end = tz.localize(datetime(2015, 1, 3))
    resolution = timedelta(minutes=15)
    target_soc_datetime = start + duration_until_target
    soc_targets = initialize_series(np.nan, start, end, resolution, inclusive="right")
    soc_targets.loc[target_soc_datetime] = target_soc
    kwargs = {
        "asset_or_sensor": charging_station,
        "start": start,
        "end": end,
        "resolution": resolution,
        "flex_model": {
            "soc_at_start": soc_at_start,
            "soc_min": charging_station.get_attribute("min_soc_in_mwh", 0),
            "soc_max": charging_station.get_attribute(
                "max_soc_in_mwh", max(soc_targets.values)
            ),
            "roundtrip_efficiency": charging_station.get_attribute(
                "roundtrip_efficiency", 1
            ),
            "storage_efficiency": charging_station.get_attribute(
                "storage_efficiency", 1
            ),
            "soc_targets": soc_targets,
        },
        "flex_context": {
            "ems_power_capacity_in_mw": ur.Quantity(f"{capacity} MVA"),
            "consumption_price": epex_da,
            "shared_currency_unit": "EUR",
        },
    }
    scheduler = StorageScheduler(**kwargs)

    # soc targets are already a DataFrame, names get underscore, shared currency unit is set
    scheduler.config_deserialized = True

    # calling the scheduler with an infeasible problem raises an Exception
    with pytest.raises(InfeasibleProblemException):
        consumption_schedule = scheduler.compute(skip_validation=True)

    # check that the fallback scheduler provides a sensible fallback policy
    fallback_scheduler = scheduler.fallback_scheduler_class(**kwargs)
    fallback_scheduler.config_deserialized = True
    consumption_schedule = fallback_scheduler.compute(skip_validation=True)

    soc_schedule = integrate_time_series(
        consumption_schedule, soc_at_start, decimal_precision=6
    )

    # Check if constraints were met
    assert min(consumption_schedule.values) >= capacity * -1
    assert max(consumption_schedule.values) <= capacity
    print(consumption_schedule.head(12))
    print(soc_schedule.head(12))
    assert (
        abs(abs(soc_schedule.loc[target_soc_datetime] - target_soc) - expected_gap)
        < TOLERANCE
    )


@pytest.mark.parametrize(
    "market_scenario",
    [
        "dynamic contract",
        "fixed contract",
    ],
)
def test_building_solver_day_2(
    db,
    add_battery_assets,
    add_market_prices,
    create_test_tariffs,
    add_inflexible_device_forecasts,
    inflexible_devices,
    flexible_devices,
    market_scenario: str,
):
    """Check battery scheduling results within the context of a building with PV, for day 2, against the following market scenarios:
    1) a dynamic tariff with equal consumption and feed-in tariffs, that is set up with 8 expensive, then 8 cheap, then again 8 expensive hours.
    2) a fixed consumption tariff and a fixed feed-in tariff that is lower, which incentives to maximize self-consumption of PV power into the battery.
    In the test data:
    - Hours with net production coincide with low dynamic market prices.
    - Hours with net consumption coincide with high dynamic market prices.
    So when the prices are low (in scenario 1), we have net production, and when they are high, net consumption.
    That means we have first net consumption, then net production, and then net consumption again.
    In either scenario, we expect the scheduler to:
    - completely discharge within the first 8 hours (either due to 1) high prices, or 2) net consumption)
    - completely charge within the next 8 hours (either due to 1) low prices, or 2) net production)
    - completely discharge within the last 8 hours (either due to 1) high prices, or 2) net consumption)
    """
    battery = flexible_devices["battery power sensor"]
    building = battery.generic_asset
    default_consumption_price_sensor = get_test_sensor(db)
    assert battery.get_attribute("consumption-price") == {
        "sensor": default_consumption_price_sensor.id
    }
    if market_scenario == "dynamic contract":
        consumption_price_sensor = default_consumption_price_sensor
        production_price_sensor = consumption_price_sensor
    elif market_scenario == "fixed contract":
        consumption_price_sensor = create_test_tariffs["consumption_price_sensor"]
        production_price_sensor = create_test_tariffs["production_price_sensor"]
    else:
        raise NotImplementedError(
            f"Missing test case for market conditions '{market_scenario}'"
        )
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 2))
    end = tz.localize(datetime(2015, 1, 3))
    resolution = timedelta(minutes=15)
    soc_at_start = 2.5
    soc_min = 0.5
    soc_max = 4.5
    scheduler = StorageScheduler(
        battery,
        start,
        end,
        resolution,
        flex_model={
            "soc_at_start": soc_at_start,
            "soc_min": soc_min,
            "soc_max": soc_max,
            "roundtrip_efficiency": battery.get_attribute("roundtrip_efficiency", 1),
            "storage_efficiency": battery.get_attribute("storage_efficiency", 1),
        },
        flex_context={
            "ems_power_capacity_in_mw": ur.Quantity("2 MVA"),
            "inflexible_device_sensors": inflexible_devices.values(),
            "production_price": production_price_sensor,
            "consumption_price": consumption_price_sensor,
            "shared_currency_unit": "EUR",
        },
    )
    # inflexible device sensors are already objects, names get underscore, shared currency unit is set
    scheduler.config_deserialized = True

    schedule = scheduler.compute()
    soc_schedule = integrate_time_series(schedule, soc_at_start, decimal_precision=6)

    with pd.option_context("display.max_rows", None, "display.max_columns", 3):
        print(soc_schedule)

    unit_factors = np.expand_dims(
        [
            convert_units(1, s.unit, "MW")
            for s in add_inflexible_device_forecasts.keys()
        ],
        axis=1,
    )
    inflexible_devices_power = np.array(list(add_inflexible_device_forecasts.values()))

    # Check if constraints were met
    capacity = pd.DataFrame(
        data=inflexible_devices_power.T.dot(
            unit_factors
        ),  # convert to MW and sum column-wise
        columns=["inflexible"],
    ).tail(
        -4 * 24
    )  # remove first 96 quarter-hours (the schedule is about the 2nd day)
    building_capacity = building.get_attribute(
        "capacity_in_mw",
        ur.Quantity(building.get_attribute("site-power-capacity")).to("MW").magnitude,
    )
    battery_capacity = battery.get_attribute(
        "capacity_in_mw"
    )  # actually a Sensor attribute
    capacity["max"] = building_capacity
    capacity["min"] = -building_capacity
    capacity["production headroom"] = capacity["max"] - capacity["inflexible"]
    capacity["consumption headroom"] = capacity["inflexible"] - capacity["min"]
    capacity["battery production headroom"] = capacity["production headroom"].clip(
        upper=battery_capacity
    )
    capacity["battery consumption headroom"] = capacity["consumption headroom"].clip(
        upper=battery_capacity
    )
    capacity["schedule"] = (
        schedule.values
    )  # consumption is positive, production is negative
    with pd.option_context(
        "display.max_rows", None, "display.max_columns", None, "display.width", 2000
    ):
        print(capacity)
    assert (capacity["schedule"] >= -capacity["battery production headroom"]).all()
    assert (capacity["schedule"] <= capacity["battery consumption headroom"]).all()

    for soc in soc_schedule.values:
        assert soc >= max(soc_min, battery.get_attribute("min_soc_in_mwh"))
        assert soc <= battery.get_attribute("max_soc_in_mwh")

    # Check whether the resulting soc schedule follows our expectations for.
    # To recap, in scenario 1 and 2, the schedule should mainly be influenced by:
    # 1) 8 expensive, 8 cheap and 8 expensive hours
    # 2) 8 net-consumption, 8 net-production and 8 net-consumption hours

    # Result after 8 hours
    # 1) Sell what you begin with
    # 2) The battery discharged as far as it could during the first 8 net-consumption hours
    assert soc_schedule.loc[start + timedelta(hours=8)] == max(
        soc_min, battery.get_attribute("min_soc_in_mwh")
    )

    # Result after second 8 hour-interval
    # 1) Buy what you can to sell later, when prices will be high again
    # 2) The battery charged with PV power as far as it could during the middle 8 net-production hours
    assert soc_schedule.loc[start + timedelta(hours=16)] == min(
        soc_max, battery.get_attribute("max_soc_in_mwh")
    )

    # Result at end of day
    # 1) The battery sold out at the end of its planning horizon
    # 2) The battery discharged as far as it could during the last 8 net-consumption hours
    assert soc_schedule.iloc[-1] == max(
        soc_min, battery.get_attribute("min_soc_in_mwh")
    )


def test_soc_bounds_timeseries(db, add_battery_assets):
    """Check that the maxima and minima timeseries alter the result
    of the optimization.

    Two schedules are run:
    - with global maximum and minimum values
    - with global maximum and minimum values +  maxima / minima time series constraints
    """

    # get the sensors from the database
    epex_da, battery = get_sensors_from_db(db, add_battery_assets)

    # time parameters
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 2))
    end = tz.localize(datetime(2015, 1, 3))
    resolution = timedelta(hours=1)

    # soc parameters
    soc_unit = "MWh"
    soc_at_start = battery.get_attribute("soc_in_mwh")
    soc_min = 0.5
    soc_max = 4.5

    def compute_schedule(flex_model):
        scheduler = StorageScheduler(
            battery,
            start,
            end,
            resolution,
            flex_model=flex_model,
        )
        schedule = scheduler.compute()

        soc_schedule = integrate_time_series(
            schedule,
            soc_at_start,
            decimal_precision=6,
        )

        return soc_schedule

    flex_model = {
        "soc-unit": soc_unit,
        "soc-at-start": soc_at_start,
        "soc-min": soc_min,
        "soc-max": soc_max,
    }

    soc_schedule_1 = compute_schedule(flex_model)

    # soc maxima and soc minima
    soc_maxima = [
        {"datetime": "2015-01-02T15:00:00+01:00", "value": 1.0},
        {"datetime": "2015-01-02T16:00:00+01:00", "value": 1.0},
    ]

    soc_minima = [{"datetime": "2015-01-02T08:00:00+01:00", "value": 3.5}]

    soc_targets = [{"datetime": "2015-01-02T19:00:00+01:00", "value": 2.0}]

    flex_model = {
        "soc-unit": soc_unit,
        "soc-at-start": soc_at_start,
        "soc-min": soc_min,
        "soc-max": soc_max,
        "soc-maxima": soc_maxima,
        "soc-minima": soc_minima,
        "soc-targets": soc_targets,
    }

    soc_schedule_2 = compute_schedule(flex_model)

    # check that, in this case, adding the constraints alters the SOC profile
    assert not soc_schedule_2.equals(soc_schedule_1)

    # check that global minimum is achieved
    assert soc_schedule_1.min() == soc_min
    assert soc_schedule_2.min() == soc_min

    # check that global maximum is achieved
    assert soc_schedule_1.max() == soc_max
    assert soc_schedule_2.max() == soc_max

    # test for soc_minima
    # check that the local minimum constraint is respected
    assert soc_schedule_2.loc["2015-01-02T08:00:00+01:00"] >= 3.5

    # test for soc_maxima
    # check that the local maximum constraint is respected
    assert soc_schedule_2.loc["2015-01-02T15:00:00+01:00"] <= 1.0

    # test for soc_targets
    # check that the SOC target (at 19 pm, local time) is met
    assert soc_schedule_2.loc["2015-01-02T19:00:00+01:00"] == 2.0


@pytest.mark.parametrize(
    "value_soc_min, value_soc_minima, value_soc_target, value_soc_maxima, value_soc_max",
    [
        (-1, -0.5, 0, 0.5, 1.0),
        (-1, -2, 0, 0.5, 1.0),
        (-1, -0.5, 0.5, 0.5, 1.0),
    ],
)
def test_add_storage_constraints(
    value_soc_min, value_soc_minima, value_soc_target, value_soc_maxima, value_soc_max
):
    """Check that the storage constraints are generated properly"""

    # from 00:00 to 04.00, both inclusive.
    start = datetime(2023, 5, 18, tzinfo=pytz.utc)
    end = datetime(2023, 5, 18, 5, tzinfo=pytz.utc)
    # hourly resolution
    resolution = timedelta(hours=1)

    soc_at_start = 0.0

    test_date = start + timedelta(hours=1)

    soc_targets = initialize_series(np.nan, start, end, resolution)
    soc_targets[test_date] = value_soc_target

    soc_maxima = initialize_series(np.nan, start, end, resolution)
    soc_maxima[test_date] = value_soc_maxima

    soc_minima = initialize_series(np.nan, start, end, resolution)
    soc_minima[test_date] = value_soc_minima

    soc_max = value_soc_max
    soc_min = value_soc_min

    storage_device_constraints = add_storage_constraints(
        start,
        end,
        resolution,
        soc_at_start,
        soc_targets,
        soc_maxima,
        soc_minima,
        soc_max,
        soc_min,
    )

    assert (storage_device_constraints["max"] <= soc_max).all()
    assert (storage_device_constraints["min"] >= soc_min).all()

    equals_not_nan = ~storage_device_constraints["equals"].isna()

    assert (storage_device_constraints["min"] <= storage_device_constraints["equals"])[
        equals_not_nan
    ].all()
    assert (storage_device_constraints["equals"] <= storage_device_constraints["max"])[
        equals_not_nan
    ].all()


@pytest.mark.parametrize(
    "value_min1, value_equals1, value_max1, value_min2, value_equals2, value_max2, expected_constraint_type_violations",
    [
        (1, np.nan, 9, 1, np.nan, 9, []),  # base case
        (1, np.nan, 10, 1, np.nan, 10, []),  # exact equality
        (
            1,
            np.nan,
            10 + 0.5e-6,
            1,
            np.nan,
            10,
            [],
        ),  # equality considering the precision (6 decimal figures)
        (
            1,
            np.nan,
            10 + 1e-5,
            1,
            np.nan,
            10,
            ["max(t) <= soc_max(t)"],
        ),  # difference of 0.5e-5 > 1e-6
        (1, np.nan, 9, 2, np.nan, 20, ["max(t) <= soc_max(t)"]),
        (-1, np.nan, 9, 1, np.nan, 9, ["soc_min(t) <= min(t)"]),
        (1, 10, 9, 1, np.nan, 9, ["equals(t) <= max(t)"]),
        (1, 0, 9, 1, np.nan, 9, ["min(t) <= equals(t)"]),
        (
            1,
            np.nan,
            9,
            9,
            np.nan,
            1,
            ["min(t) <= max(t)"],
        ),
        (
            9,
            5,
            1,
            1,
            np.nan,
            9,
            ["min(t) <= equals(t)", "equals(t) <= max(t)", "min(t) <= max(t)"],
        ),
        (1, np.nan, 9, 1, np.nan, 9, []),  # same interval, should not fail
        (1, np.nan, 9, 3, np.nan, 7, []),  # should not fail, containing interval
        (1, np.nan, 3, 3, np.nan, 5, []),  # difference = 0 < 1, should not fail
        (1, np.nan, 3, 4, np.nan, 5, []),  # difference == max, should not fail
        (
            1,
            np.nan,
            3,
            5,
            np.nan,
            7,
            ["min(t) - max(t-1) <= derivative_max(t) * factor_w_wh(t)"],
        ),  # difference > max = 1, this should fail
        (3, np.nan, 5, 2, np.nan, 3, []),  # difference = 0 < 1, should not fail
        (3, np.nan, 5, 1, np.nan, 2, []),  # difference = -1 >= -1, should not fail
        (
            3,
            np.nan,
            5,
            1,
            np.nan,
            1,
            ["derivative_min(t) * factor_w_wh(t) <= max(t) - min(t-1)"],
        ),  # difference = -2 < -1, should fail,
        (1, 4, 9, 1, 4, 9, []),  # same target value (4), should not fail
        (
            1,
            6,
            9,
            1,
            4,
            9,
            ["derivative_min(t) * factor_w_wh(t) <= equals(t) - equals(t-1)"],
        ),  # difference = -2 < -1, should fail,
        (
            1,
            4,
            9,
            1,
            6,
            9,
            ["equals(t) - equals(t-1) <= derivative_max(t) * factor_w_wh(t)"],
        ),  # difference 2 > 1, should fail,
    ],
)
def test_validate_constraints(
    value_min1,
    value_equals1,
    value_max1,
    value_min2,
    value_equals2,
    value_max2,
    expected_constraint_type_violations,
):
    """Check the validation of constraints.
    Two consecutive SOC ranges are parametrized (min, equals, max) and the different conditions are tested.
    """
    # from 00:00 to 04.00, both inclusive.
    start = datetime(2023, 5, 18, tzinfo=pytz.utc)
    end = datetime(2023, 5, 18, 5, tzinfo=pytz.utc)

    # hourly resolution
    resolution = timedelta(hours=1)

    columns = ["equals", "max", "min", "derivative max", "derivative min"]

    storage_device_constraints = initialize_df(columns, start, end, resolution)

    test_time = start + resolution * 2

    storage_device_constraints["min"] = 0
    storage_device_constraints["max"] = 10

    storage_device_constraints["derivative max"] = 1
    storage_device_constraints["derivative min"] = -1

    storage_device_constraints.loc[
        storage_device_constraints.index == test_time, "min"
    ] = value_min1
    storage_device_constraints.loc[
        storage_device_constraints.index == test_time, "max"
    ] = value_max1
    storage_device_constraints.loc[
        storage_device_constraints.index == test_time, "equals"
    ] = value_equals1

    storage_device_constraints.loc[
        storage_device_constraints.index == test_time + resolution, "min"
    ] = value_min2
    storage_device_constraints.loc[
        storage_device_constraints.index == test_time + resolution, "max"
    ] = value_max2
    storage_device_constraints.loc[
        storage_device_constraints.index == test_time + resolution, "equals"
    ] = value_equals2

    constraint_violations = validate_storage_constraints(
        constraints=storage_device_constraints,
        soc_at_start=0.0,
        soc_min=0,
        soc_max=10,
        resolution=resolution,
    )

    constraint_type_violations_output = set(
        constraint_violation["condition"]
        for constraint_violation in constraint_violations
    )

    assert set(expected_constraint_type_violations) == constraint_type_violations_output


def test_infeasible_problem_error(db, add_battery_assets):
    """Try to create a schedule with infeasible constraints. soc-max is 4.5 and soc-target is 8.0"""

    # get the sensors from the database
    _epex_da, battery = get_sensors_from_db(db, add_battery_assets)

    # time parameters
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 2))
    end = tz.localize(datetime(2015, 1, 3))
    resolution = timedelta(hours=1)

    def compute_schedule(flex_model):
        scheduler = StorageScheduler(
            battery,
            start,
            end,
            resolution,
            flex_model=flex_model,
        )
        schedule = scheduler.compute()

        soc_schedule = integrate_time_series(
            schedule,
            soc_at_start,
            decimal_precision=1,
        )

        return soc_schedule

    # soc parameters
    soc_at_start = battery.get_attribute("soc_in_mwh")
    infeasible_max_soc_targets = [
        {"datetime": "2015-01-02T16:00:00+01:00", "value": 8.0}
    ]

    flex_model = {
        "soc-at-start": soc_at_start,
        "soc-min": 0.5,
        "soc-max": 4.5,
        "soc-targets": infeasible_max_soc_targets,
    }

    with pytest.raises(
        ValueError, match="The input data yields an infeasible problem."
    ):
        compute_schedule(flex_model)


def test_numerical_errors(app_with_each_solver, setup_planning_test_data, db):
    """Test that a soc-target = soc-max can exceed this value due to numerical errors in the operations
    to compute the device constraint DataFrame.
    In the case of HiGHS, the tiny difference creates an infeasible constraint.
    """

    epex_da = get_test_sensor(db)
    charging_station = setup_planning_test_data[
        "Test charging station (bidirectional)"
    ].sensors[0]
    capacity = charging_station.get_attribute(
        "capacity_in_mw",
        ur.Quantity(charging_station.get_attribute("site-power-capacity"))
        .to("MW")
        .magnitude,
    )
    assert capacity == 2
    assert charging_station.get_attribute("consumption-price") == {"sensor": epex_da.id}

    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 2))
    end = tz.localize(datetime(2015, 1, 3))
    resolution = timedelta(minutes=5)

    duration_until_next_target = timedelta(hours=1)
    target_soc_datetime_1 = pd.Timestamp(start + duration_until_next_target).isoformat()
    target_soc_datetime_2 = pd.Timestamp(
        start + 2 * duration_until_next_target
    ).isoformat()

    scheduler = StorageScheduler(
        charging_station,
        start,
        end,
        resolution,
        flex_model={
            "soc-at-start": 0.01456,
            "soc-min": 0.01295,
            "soc-max": 0.056,
            "roundtrip-efficiency": 0.85,
            "storage-efficiency": 1,
            "soc-targets": [
                {"value": 0.01295, "datetime": target_soc_datetime_1},
                {"value": 0.056, "datetime": target_soc_datetime_2},
            ],
            "soc-unit": "MWh",
        },
    )

    (
        sensors,
        start,
        end,
        resolution,
        soc_at_start,
        device_constraints,
        ems_constraints,
        commitments,
    ) = scheduler._prepare(skip_validation=True)

    _, _, results, model = device_scheduler(
        device_constraints=device_constraints,
        ems_constraints=ems_constraints,
        commitments=commitments,
        initial_stock=[
            soc_at_start_d * (timedelta(hours=1) / resolution)
            for soc_at_start_d in soc_at_start
        ],
    )
    assert results.solver.termination_condition == "optimal"

    assert device_constraints[0]["equals"].max() > device_constraints[0]["max"].max()
    assert device_constraints[0]["equals"].min() < device_constraints[0]["min"].min()
    assert results.solver.status == "ok"


@pytest.mark.parametrize(
    "power_sensor_name,capacity,expected_capacity,site_capacity,site_consumption_capacity,site_production_capacity,expected_site_consumption_capacity, expected_site_production_capacity",
    [
        (
            "Battery (with symmetric site limits)",
            "100kW",
            0.1,
            "300kW",
            None,
            None,
            0.3,
            -0.3,
        ),
        (
            "Battery (with symmetric site limits)",
            "0.2 MW",
            0.2,
            "42 kW",
            None,
            None,
            0.042,
            -0.042,
        ),
        (
            "Battery (with symmetric site limits)",
            "0.2 MW",
            0.2,
            "42 kW",
            "10kW",
            "100kW",
            0.01,
            -0.042,
        ),
        (
            "Battery (with symmetric site limits)",
            "0.2 MW",
            0.2,
            None,
            "10kW",
            "100kW",
            0.01,
            -0.1,
        ),
        (
            "Battery (with symmetric site limits)",
            "0.2 MW",
            0.2,
            None,
            None,
            None,
            2,
            -2,
        ),
        (
            "Battery (with asymmetric site limits)",
            "0.2 MW",
            0.2,
            None,
            None,
            None,
            0.9,
            -0.75,
        ),  # default from the asset attributes
        (
            "Battery (with asymmetric site limits)",
            "0.2 MW",
            0.2,
            "42 kW",
            None,
            None,
            0.042,
            -0.042,
        ),
        (
            "Battery (with asymmetric site limits)",
            "0.2 MW",
            0.2,
            "42 kW",
            "30kW",
            None,
            0.03,
            -0.042,
        ),
        (
            "Battery (with asymmetric site limits)",
            "0.2 MW",
            0.2,
            "42 kW",
            None,
            "30kW",
            0.042,
            -0.03,
        ),
    ],
)
def test_capacity(
    app,
    power_sensor_name,
    add_assets_with_site_power_limits,
    add_market_prices,
    capacity,
    expected_capacity,
    site_capacity,
    site_consumption_capacity,
    site_production_capacity,
    expected_site_consumption_capacity,
    expected_site_production_capacity,
):
    """Test that the power limits of the site and storage device are set properly using the
    flex-model and flex-context.

    Priority for fetching the production and consumption capacity:

    "site-{direction}-capacity" (flex-context) -> "site-power-capacity" (flex-context) ->
    "{direction}_capacity_in_mw" (asset attribute) -> "capacity_in_mw" (asset attribute)

    direction = "production" or "consumption"
    """

    flex_model = {
        "soc-at-start": 0.01,
    }

    flex_context = {
        "production-price": {"sensor": add_market_prices["epex_da_production"].id},
        "consumption-price": {"sensor": add_market_prices["epex_da"].id},
    }

    def set_if_not_none(dictionary, key, value):
        if value is not None:
            dictionary[key] = value

    set_if_not_none(flex_model, "power-capacity", capacity)
    set_if_not_none(flex_context, "site-power-capacity", site_capacity)
    set_if_not_none(
        flex_context, "site-consumption-capacity", site_consumption_capacity
    )
    set_if_not_none(flex_context, "site-production-capacity", site_production_capacity)

    device = add_assets_with_site_power_limits[power_sensor_name]

    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 3))
    end = tz.localize(datetime(2015, 1, 4))
    resolution = timedelta(minutes=5)

    scheduler = StorageScheduler(
        device,
        start,
        end,
        resolution,
        flex_model=flex_model,
        flex_context=flex_context,
    )

    (
        sensors,
        start,
        end,
        resolution,
        soc_at_start,
        device_constraints,
        ems_constraints,
        commitments,
    ) = scheduler._prepare(skip_validation=True)

    assert all(device_constraints[0]["derivative min"] == -expected_capacity)
    assert all(device_constraints[0]["derivative max"] == expected_capacity)

    assert all(ems_constraints["derivative min"] == expected_site_production_capacity)
    assert all(ems_constraints["derivative max"] == expected_site_consumption_capacity)


@pytest.mark.parametrize(
    ["soc_values", "log_message", "expected_num_targets"],
    [
        (
            [
                {
                    "start": datetime(2023, 5, 19, tzinfo=pytz.utc),
                    "end": datetime(2023, 5, 23, tzinfo=pytz.utc),
                    "value": 1.0,
                },
            ],
            "Disregarding target datetimes that exceed 2023-05-20 00:00:00+00:00 (within the window 2023-05-19 00:00:00+00:00 until 2023-05-23 00:00:00+00:00).",
            1 * 24 * 60 / 5
            + 1,  # every 5-minute mark on the 19th including both midnights
        ),
        (
            [
                {"datetime": datetime(2023, 5, 19, tzinfo=pytz.utc), "value": 1.0},
                {"datetime": datetime(2023, 5, 23, tzinfo=pytz.utc), "value": 1.0},
            ],
            "Disregarding target datetime 2023-05-23 00:00:00+00:00, because it exceeds 2023-05-20 00:00:00+00:00.",
            1,  # only the 19th
        ),
        (
            [
                {"datetime": datetime(2023, 5, 19, tzinfo=pytz.utc), "value": 1.0},
                {"datetime": datetime(2023, 5, 22, tzinfo=pytz.utc), "value": 1.0},
                {"datetime": datetime(2023, 5, 23, tzinfo=pytz.utc), "value": 1.0},
                {"datetime": datetime(2023, 5, 21, tzinfo=pytz.utc), "value": 1.0},
            ],
            "Disregarding target datetimes that exceed 2023-05-20 00:00:00+00:00 (within the window 2023-05-21 00:00:00+00:00 until 2023-05-23 00:00:00+00:00 spanning 3 targets).",
            1,  # only the 19th
        ),
    ],
)
def test_build_device_soc_values(caplog, soc_values, log_message, expected_num_targets):
    caplog.set_level(logging.WARNING)
    soc_at_start = 3.0
    start_of_schedule = datetime(2023, 5, 18, tzinfo=pytz.utc)
    end_of_schedule = datetime(2023, 5, 20, tzinfo=pytz.utc)
    resolution = timedelta(minutes=5)

    # Convert SoC datetimes to periods with a start and end.
    for soc in soc_values:
        TimedEventSchema().check_time_window(soc)

    with caplog.at_level(logging.WARNING):
        device_values = build_device_soc_values(
            soc_values=soc_values,
            soc_at_start=soc_at_start,
            start_of_schedule=start_of_schedule,
            end_of_schedule=end_of_schedule,
            resolution=resolution,
        )
    print(device_values)
    assert log_message in caplog.text

    # Check test assumption
    for soc in soc_values:
        assert soc["value"] == 1
    soc_delta = 1 - soc_at_start
    soc_delta_per_resolution = soc_delta * timedelta(hours=1) / resolution

    assert soc_delta_per_resolution in device_values
    assert np.count_nonzero(~np.isnan(device_values)) == expected_num_targets


@pytest.mark.parametrize(
    [
        "battery_name",
        "production_sensor",
        "consumption_sensor",
        "production_quantity",
        "consumption_quantity",
        "expected_production",
        "expected_consumption",
        "site_production_capacity_sensor",
        "site_consumption_capacity_sensor",
        "expected_site_production",
        "expected_site_consumption",
    ],
    [
        (
            "Test battery with dynamic power capacity",
            False,
            False,
            None,
            None,
            -8,  # from the power sensor attribute 'production_capacity'
            0.5,  # from the power sensor attribute 'consumption_capacity'
            False,
            False,
            -1.1,
            1.1,
        ),
        (
            "Test battery with dynamic power capacity",
            True,
            False,
            None,
            None,
            # from the flex model field 'production-capacity' (a sensor),
            # and when absent, defaulting to the max value from the power sensor attribute capacity_in_mw
            [-0.2] * 4 * 4 + [-0.3] * 4 * 4 + [-10] * 16 * 4,
            # from the power sensor attribute 'consumption_capacity'
            0.5,
            False,
            False,
            -1.1,
            1.1,
        ),
        (
            "Test battery with dynamic power capacity",
            False,
            True,
            None,
            None,
            # from the power sensor attribute 'consumption_capacity'
            [-8] * 24 * 4,
            # from the flex model field 'consumption-capacity' (a sensor),
            # and when absent, defaulting to the max value from the power sensor attribute capacity_in_mw
            [0.25] * 4 * 4 + [0.15] * 4 * 4 + [10] * 16 * 4,
            False,
            True,
            -1.1,
            # the first period with consumption capacity of 1.3 MVA is clipped by a site-power-capacity of 1.1 MVA,
            # the second period with consumption capacity of 1.05 MVA is kept as is, and
            # the third period with missing consumption capacity defaults to 1.1 MVA
            [1.1] * 4 * 4 + [1.05] * 4 * 4 + [1.1] * 16 * 4,
        ),
        (
            "Test battery with dynamic power capacity",
            False,
            False,
            "100 kW",
            "200 kW",
            # from the flex model field 'production-capacity' (a quantity)
            -0.1,
            # from the flex model field 'consumption-capacity' (a quantity)
            0.2,
            False,
            False,
            -1.1,
            1.1,
        ),
        (
            "Test battery with dynamic power capacity",
            False,
            False,
            "1 MW",
            "2 MW",
            # from the flex model field 'production-capacity' (a quantity)
            -1,
            # from the power sensor attribute 'consumption_capacity' (a quantity)
            2,
            False,
            False,
            -1.1,
            1.1,
        ),
        (
            "Test battery",
            False,
            False,
            None,
            None,
            # from the asset attribute 'capacity_in_mw'
            -2,
            # from the asset attribute 'capacity_in_mw'
            2,
            False,
            False,
            -1.1,
            1.1,
        ),
        (
            "Test battery",
            False,
            False,
            "10 kW",
            None,
            # from the flex model field 'production-capacity' (a quantity)
            -0.01,
            # from the asset attribute 'capacity_in_mw'
            2,
            False,
            False,
            -1.1,
            1.1,
        ),
        (
            "Test battery",
            False,
            False,
            "10 kW",
            "100 kW",
            # from the flex model field 'production-capacity' (a quantity)
            -0.01,
            # from the flex model field 'consumption-capacity' (a quantity)
            0.1,
            True,
            True,
            [-1.1] * 4 * 4 + [-1.05] * 4 * 4 + [-1.1] * 16 * 4,
            [1.1] * 4 * 4 + [1.05] * 4 * 4 + [1.1] * 16 * 4,
        ),
    ],
)
def test_battery_power_capacity_as_sensor(
    db,
    add_battery_assets,
    add_inflexible_device_forecasts,
    capacity_sensors,
    battery_name,
    production_sensor,
    consumption_sensor,
    production_quantity,
    consumption_quantity,
    expected_production,
    expected_consumption,
    site_consumption_capacity_sensor,
    site_production_capacity_sensor,
    expected_site_production,
    expected_site_consumption,
):
    epex_da, battery = get_sensors_from_db(
        db, add_battery_assets, battery_name=battery_name
    )

    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 2))
    end = tz.localize(datetime(2015, 1, 3))
    resolution = timedelta(minutes=15)
    soc_at_start = 10

    flex_context = {"site-power-capacity": "1100 kVA"}  # 1.1 MW
    if site_consumption_capacity_sensor:
        flex_context["site-consumption-capacity"] = {
            "sensor": capacity_sensors["site_power_capacity"].id
        }
    if site_production_capacity_sensor:
        flex_context["site-production-capacity"] = {
            "sensor": capacity_sensors["site_power_capacity"].id
        }

    flex_model = {
        "soc-at-start": soc_at_start,
        "roundtrip-efficiency": "100%",
        "prefer-charging-sooner": False,
    }

    if production_sensor:
        flex_model["production-capacity"] = {
            "sensor": capacity_sensors["production"].id
        }

    if consumption_sensor:
        flex_model["consumption-capacity"] = {
            "sensor": capacity_sensors["consumption"].id
        }

    if production_quantity is not None:
        flex_model["production-capacity"] = production_quantity

    if consumption_quantity is not None:
        flex_model["consumption-capacity"] = consumption_quantity

    scheduler: Scheduler = StorageScheduler(
        battery,
        start,
        end,
        resolution,
        flex_model=flex_model,
        flex_context=flex_context,
    )

    data_to_solver = scheduler._prepare()
    device_constraints = data_to_solver[5][0]
    ems_constraints = data_to_solver[6]

    assert all(device_constraints["derivative min"].values == expected_production)
    assert all(device_constraints["derivative max"].values == expected_consumption)
    assert all(ems_constraints["derivative min"].values == expected_site_production)
    assert all(ems_constraints["derivative max"].values == expected_site_consumption)


def test_battery_bothways_power_capacity_as_sensor(
    db, add_battery_assets, add_inflexible_device_forecasts, capacity_sensors
):
    """Check that the charging and discharging power capacities are limited by the power capacity."""
    epex_da, battery = get_sensors_from_db(
        db, add_battery_assets, battery_name="Test battery"
    )

    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 2))
    end = tz.localize(datetime(2015, 1, 2, 7, 45))
    resolution = timedelta(minutes=15)
    soc_at_start = 10

    flex_model = {
        "soc-at-start": soc_at_start,
        "roundtrip-efficiency": "100%",
        "prefer-charging-sooner": False,
    }

    flex_model["power-capacity"] = {"sensor": capacity_sensors["production"].id}
    flex_model["consumption-capacity"] = {"sensor": capacity_sensors["consumption"].id}
    flex_model["production-capacity"] = {
        "sensor": capacity_sensors["power_capacity"].id
    }

    scheduler: Scheduler = StorageScheduler(
        battery, start, end, resolution, flex_model=flex_model
    )

    data_to_solver = scheduler._prepare()
    device_constraints = data_to_solver[5][0]

    max_capacity = (
        capacity_sensors["power_capacity"]
        .search_beliefs(event_starts_after=start, event_ends_before=end)
        .event_value.values
    )

    assert all(device_constraints["derivative min"].values >= -max_capacity)
    assert all(device_constraints["derivative max"].values <= max_capacity)


def get_efficiency_problem_device_constraints(
    extra_flex_model, efficiency_sensors, add_battery_assets, db
) -> pd.DataFrame:
    _, battery = get_sensors_from_db(db, add_battery_assets)
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 1))
    end = tz.localize(datetime(2015, 1, 2))
    resolution = timedelta(minutes=15)
    base_flex_model = {
        "soc-max": 2,
        "soc-min": 0,
    }
    base_flex_model.update(extra_flex_model)
    scheduler: Scheduler = StorageScheduler(
        battery, start, end, resolution, flex_model=base_flex_model
    )

    scheduler_data = scheduler._prepare()
    return scheduler_data[5][0]


def test_dis_charging_efficiency_as_sensor(
    db,
    add_battery_assets,
    add_inflexible_device_forecasts,
    add_market_prices,
    efficiency_sensors,
):
    """
    Check that the charging and discharging efficiency can be defined in the flex-model.
    For missing values, the fallback value is 100%.
    """

    tz = pytz.timezone("Europe/Amsterdam")

    end = tz.localize(datetime(2015, 1, 2))
    resolution = timedelta(minutes=15)

    extra_flex_model = {
        "charging-efficiency": {"sensor": efficiency_sensors["efficiency"].id},
        "discharging-efficiency": "200%",
    }

    device_constraints = get_efficiency_problem_device_constraints(
        extra_flex_model, efficiency_sensors, add_battery_assets, db
    )

    assert all(
        device_constraints[: end - timedelta(hours=1) - resolution][
            "derivative up efficiency"
        ]
        == 0.9
    )
    assert all(
        device_constraints[end - timedelta(hours=1) :]["derivative up efficiency"] == 1
    )

    assert all(device_constraints["derivative down efficiency"] == 2)


@pytest.mark.parametrize(
    "stock_delta_sensor",
    ["delta fails", "delta", "delta hourly", "delta 5min", None],
)
def test_battery_stock_delta_sensor(
    add_battery_assets, add_stock_delta, stock_delta_sensor, db
):
    """
    Test the SOC delta feature using sensors.

    An empty battery is made to fulfill a usage signal under a flat tariff.
    The battery is only allowed to charge (production-capacity = 0).

    Set up the same constant delta (capacity_in_mw) in different resolutions.

    The problem is defined with the following settings:
        - Battery empty at the start of the schedule (soc-at-start = 0).
        - Battery of size 2 MWh.
        - Consumption capacity of the battery is 2 MW.
        - The battery cannot discharge.
    With these settings, the battery needs to charge at a power or greater than the usage forecast
    to keep the SOC within bounds ([0, 2 MWh]).
    """
    _, battery = get_sensors_from_db(db, add_battery_assets)
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 1))
    end = tz.localize(datetime(2015, 1, 2))
    resolution = timedelta(minutes=15)
    if stock_delta_sensor is not None:
        stock_delta_sensor_obj = add_stock_delta[stock_delta_sensor]
    else:
        stock_delta_sensor_obj = add_stock_delta["delta"]
    capacity = stock_delta_sensor_obj.get_attribute(
        "capacity_in_mw",
        ur.Quantity(stock_delta_sensor_obj.get_attribute("site-power-capacity"))
        .to("MW")
        .magnitude,
    )
    flex_model = {
        "soc-max": 2,
        "soc-min": 0,
        "roundtrip-efficiency": 1,
        "storage-efficiency": 1,
        "production-capacity": "0kW",
        "soc-at-start": 0,
    }
    if stock_delta_sensor is not None:
        flex_model = {
            **flex_model,
            **{"soc-usage": [{"sensor": stock_delta_sensor_obj.id}]},
        }

    scheduler: Scheduler = StorageScheduler(
        battery,
        start,
        end,
        resolution,
        flex_model=flex_model,
    )

    if stock_delta_sensor == "delta fails":
        with pytest.raises(InfeasibleProblemException):
            scheduler.compute()
    elif stock_delta_sensor is None:
        # No usage -> the battery does not charge
        schedule = scheduler.compute()
        assert all(schedule == 0)
    else:
        # Some usage -> the battery needs to charge
        schedule = scheduler.compute()
        assert all(schedule == capacity)


@pytest.mark.parametrize(
    "gain,usage,expected_delta",
    [
        (["1 MW"], ["1MW"], 0),  # delta stock is 0 (1 MW - 1 MW)
        (["0.5 MW", "0.5 MW"], None, 1),  # 1 MW stock gain
        (["100 kW"], None, 0.1),  # 100 MW stock gain
        (None, ["100 kW"], -0.1),  # 100 kW stock usage
        (None, None, 0),  # no gain/usage defined -> the fallback usage is set to 0
    ],
)
def test_battery_stock_delta_quantity(
    add_battery_assets, gain, usage, expected_delta, db
):
    """
    Test the stock gain field when a constant value is provided.

    We expect a constant gain/usage to happen in every time period equal to the energy
    value provided.
    """
    _, battery = get_sensors_from_db(db, add_battery_assets)
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 1))
    end = tz.localize(datetime(2015, 1, 2))
    resolution = timedelta(minutes=15)
    flex_model = {
        "soc-max": 2,
        "soc-min": 0,
        "roundtrip-efficiency": 1,
        "storage-efficiency": 1,
    }

    if gain is not None:
        flex_model["soc-gain"] = gain
    if usage is not None:
        flex_model["soc-usage"] = usage

    scheduler: Scheduler = StorageScheduler(
        battery, start, end, resolution, flex_model=flex_model
    )
    scheduler_info = scheduler._prepare()

    if expected_delta is not None:
        assert all(scheduler_info[5][0]["stock delta"] == expected_delta)
    else:
        assert all(scheduler_info[5][0]["stock delta"].isna())


@pytest.mark.parametrize(
    "efficiency,expected_efficiency",
    [
        ("100%", 1),
        (
            "110%",
            1,
        ),  # value exceeding the upper bound (100%). It clips the values above 100%.
        (
            "90%",
            0.9,
        ),  # percentage unit to dimensionless units within the [0,1] interval
        (0.9, 0.9),  # numeric values are interpreted as dimensionless
        (
            None,
            None,
        ),  # if the `storage-efficiency` is not defined, the constraint dataframe
        # uses NaN.
    ],
)
def test_battery_efficiency_quantity(
    add_battery_assets, efficiency, expected_efficiency, db
):
    """
    Test to ensure correct handling of storage efficiency quantities in the StorageScheduler.

    The test covers the handling of percentage values, dimensionless numeric values, and the
    case where the efficiency is not defined.
    """

    _, battery = get_sensors_from_db(db, add_battery_assets)
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 1))
    end = tz.localize(datetime(2015, 1, 2))
    resolution = timedelta(minutes=15)
    flex_model = {
        "soc-max": 2,
        "soc-min": 0,
        "roundtrip-efficiency": 1,
    }

    if efficiency is not None:
        flex_model["storage-efficiency"] = efficiency

    scheduler: Scheduler = StorageScheduler(
        battery, start, end, resolution, flex_model=flex_model
    )
    scheduler_info = scheduler._prepare()

    if efficiency is not None:
        assert all(scheduler_info[5][0]["efficiency"] == expected_efficiency)
    else:
        assert all(scheduler_info[5][0]["efficiency"].isna())


@pytest.mark.parametrize(
    "efficiency_sensor_name, expected_efficiency",
    [
        ("storage efficiency 90%", 0.9),  # regular value
        ("storage efficiency 110%", 1),  # clip values that exceed 100%
        ("storage efficiency negative", 0),  # clip negative values
        pytest.param(
            "storage efficiency hourly",
            0.974003,
            marks=pytest.mark.xfail(
                reason="resampling storage efficiency is not supported"
            ),
        ),  # this one fails.
        # We should resample making sure that the efficiencies are equivalent.
        # For example, 90% defined in 1h is equivalent to 97% in a 15min period (0.97^4≈0.9).
        # Plans to support resampling efficiencies can be found here https://github.com/FlexMeasures/flexmeasures/issues/720
    ],
)
def test_battery_storage_efficiency_sensor(
    add_battery_assets,
    add_storage_efficiency,
    efficiency_sensor_name,
    expected_efficiency,
    db,
):
    """
    Test the handling of different storage efficiency sensors in the StorageScheduler.

    It checks if the scheduler correctly handles regular values, values exceeding 100%, negative values,
    and values with different resolutions compared to the scheduling resolution.
    """
    _, battery = get_sensors_from_db(db, add_battery_assets)
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 1))
    end = tz.localize(datetime(2015, 1, 2))
    resolution = timedelta(minutes=15)
    storage_efficiency_sensor_obj = add_storage_efficiency[efficiency_sensor_name]

    scheduler: Scheduler = StorageScheduler(
        battery,
        start,
        end,
        resolution,
        flex_model={
            "soc-max": 2,
            "soc-min": 0,
            "roundtrip-efficiency": 1,
            "storage-efficiency": {"sensor": storage_efficiency_sensor_obj.id},
            "production-capacity": "0kW",
            "soc-at-start": 0,
        },
    )

    scheduler_info = scheduler._prepare()
    assert all(scheduler_info[5][0]["efficiency"] == expected_efficiency)


@pytest.mark.parametrize(
    "sensor_name, expected_start, expected_end, n_constraints",
    [
        # A value defined in a coarser resolution is upsampled to match the power sensor resolution.
        (
            "soc-targets (1h)",
            "14:00:00",
            "15:00:00",
            5,
        ),
        # A value defined in a finer resolution is downsampled to match the power sensor resolution.
        # Only a single value coincides with the power sensor resolution.
        pytest.param(
            "soc-targets (5min)",
            "14:00:00",
            "14:00:00",  # not "14:15:00"
            1,
            marks=pytest.mark.xfail(
                reason="timely-beliefs doesn't yet make it possible to resample to a certain frequency and event resolution simultaneously"
            ),
        ),
        # A simple case, SOC constraint sensor in the same resolution as the power sensor.
        (
            "soc-targets (15min)",
            "14:00:00",
            "14:15:00",
            2,
        ),
        # For an instantaneous sensor, the value is set to the interval containing the instantaneous event.
        (
            "soc-targets (instantaneous)",
            "14:00:00",
            "14:00:00",
            1,
        ),
        # This is an event at 14:05:00 with a duration of 15min.
        # This constraint should span the intervals from 14:00 to 14:15 and from 14:15 to 14:30, but we are not reindexing properly.
        pytest.param(
            "soc-targets (15min lagged)",
            "14:00:00",
            "14:15:00",
            1,
            marks=pytest.mark.xfail(
                reason="we should re-index the series so that values of the original index that overlap are used."
            ),
        ),
    ],
)
def test_add_storage_constraint_from_sensor(
    add_battery_assets,
    add_soc_targets,
    sensor_name,
    expected_start,
    expected_end,
    n_constraints,
    db,
):
    """
    Test the handling of different values for the target SOC constraints as sensors in the StorageScheduler.
    """
    _, battery = get_sensors_from_db(db, add_battery_assets)
    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 1))
    end = tz.localize(datetime(2015, 1, 2))
    resolution = timedelta(minutes=15)
    soc_targets = add_soc_targets[sensor_name]
    soc_at_start = 0

    flex_model = {
        "soc-max": 2,
        "soc-min": 0,
        "roundtrip-efficiency": 1,
        "production-capacity": "0kW",
        "soc-at-start": soc_at_start,
    }

    flex_model["soc-targets"] = {"sensor": soc_targets.id}
    flex_model["soc-maxima"] = [
        {
            "datetime": "2015-01-01T13:45:00+01:00",
            "value": "0.4 MWh",
        }
    ]

    scheduler: Scheduler = StorageScheduler(
        battery, start, end, resolution, flex_model=flex_model
    )

    scheduler_info = scheduler._prepare()
    storage_constraints = scheduler_info[5][0]

    # Start (date) + start (time) - resolution (due to device_constraints indexing states by the start of their preceding time slot)
    expected_target_start = start + pd.Timedelta(expected_start) - resolution
    expected_target_end = start + pd.Timedelta(expected_end) - resolution
    expected_soc_target_value = 0.5 * timedelta(hours=1) / resolution

    # convert dates from UTC to local time (Europe/Amsterdam)
    equals = storage_constraints["equals"].tz_convert(tz)

    # check that no value before expected_target_start is non-nan
    assert all(equals[: expected_target_start - resolution].isna())

    # check that no value after expected_target_end is non-nan
    assert all(equals[expected_target_end + resolution :].isna())

    # check that the values in the (expected_target_start, expected_target_end) are equal to the expected value
    assert all(
        equals[expected_target_start + resolution : expected_target_end]
        == expected_soc_target_value
    )

    # Check the resulting schedule against the constraints
    consumption_schedule = scheduler.compute()
    soc_schedule = integrate_time_series(
        consumption_schedule, soc_at_start, decimal_precision=6
    )
    # Note the equality constraints are shifted back to account for how they define the index to denote
    # the start of the event that ends in the given equality state, whereas the index of the soc_schedule
    # denotes the exact time of the given SoC state
    comparison_df = pd.concat([equals.shift(1), soc_schedule], axis=1).dropna()
    assert (
        len(comparison_df)
    ) == n_constraints, f"we expect {n_constraints} device constraints"
    assert all(
        comparison_df.iloc[:, 0] == comparison_df.iloc[:, 1] * 4
    ), "the device constraint values should be 1/4th of the actual SoC values, due to the 15-minute resolution of the battery's power sensor"


def test_soc_maxima_minima_targets(db, add_battery_assets, soc_sensors):
    """
    Check that the SOC maxima, minima and targets can be defined as sensors in the StorageScheduler.

    The SOC is forced to follow a certain trajectory both by means of the SOC target and by setting SOC maxima = SOC minima = SOC targets.

    Moreover, the SOC maxima constraints are defined in MWh to check that the unit conversion works well.
    """
    power = add_battery_assets["Test battery with dynamic power capacity"].sensors[0]
    epex_da = get_test_sensor(db)

    soc_maxima, soc_minima, soc_targets, expected_soc_schedule = soc_sensors

    tz = pytz.timezone("Europe/Amsterdam")
    start = tz.localize(datetime(2015, 1, 1))
    end = tz.localize(datetime(2015, 1, 2))
    resolution = timedelta(minutes=15)
    soc_at_start = 0.0
    soc_max = 10
    soc_min = 0

    flex_model = {
        "soc-at-start": soc_at_start,
        "soc-max": soc_max,
        "soc-min": soc_min,
        "power-capacity": "2 MW",
        "production-capacity": "2 MW",
        "consumption-capacity": "2 MW",
        "storage-efficiency": 1,
        "charging-efficiency": "100%",
        "discharging-efficiency": "100%",
    }

    def compute_schedule(flex_model):
        scheduler: Scheduler = StorageScheduler(
            power,
            start,
            end,
            resolution,
            flex_model=flex_model,
            flex_context={
                "site-power-capacity": "100 MW",
                "production-price": {"sensor": epex_da.id},
                "consumption-price": {"sensor": epex_da.id},
            },
        )
        return scheduler.compute()

    flex_model["soc-targets"] = {"sensor": soc_targets.id}
    schedule = compute_schedule(flex_model)

    soc = check_constraints(power, schedule, soc_at_start)

    # soc targets are achieved
    assert all(abs(soc[8:].values - expected_soc_schedule) < 1e-5)

    # remove soc-targets and use soc-maxima and soc-minima
    del flex_model["soc-targets"]
    flex_model["soc-minima"] = {"sensor": soc_minima.id}
    flex_model["soc-maxima"] = {"sensor": soc_maxima.id}
    schedule = compute_schedule(flex_model)

    soc = check_constraints(power, schedule, soc_at_start)

    # soc-maxima and soc-minima constraints are respected
    # this yields the same results as with the SOC targets
    # because soc-maxima = soc-minima = soc-targets
    assert all(abs(soc[8:].values - expected_soc_schedule) < 1e-5)


@pytest.mark.parametrize("unit", [None, "MWh", "kWh"])
@pytest.mark.parametrize("soc_unit", ["kWh", "MWh"])
@pytest.mark.parametrize("power_sensor_name", ["power", "power (kW)"])
def test_battery_storage_different_units(
    add_battery_assets,
    db,
    power_sensor_name,
    soc_unit,
    unit,
):
    """
    Test scheduling a 1 MWh battery for 2h with a low -> high price transition with
    different units for the soc-min, soc-max, soc-at-start and power sensor.
    """

    soc_min = ur.Quantity("100 kWh")
    soc_max = ur.Quantity("1 MWh")
    soc_at_start = ur.Quantity("100 kWh")

    if unit is not None:
        soc_min = str(soc_min.to(unit))
        soc_max = str(soc_max.to(unit))
        soc_at_start = str(soc_at_start.to(unit))
    else:
        soc_min = soc_min.to(soc_unit).magnitude
        soc_max = soc_max.to(soc_unit).magnitude
        soc_at_start = soc_at_start.to(soc_unit).magnitude

    epex_da, battery = get_sensors_from_db(
        db,
        add_battery_assets,
        battery_name="Test battery",
        power_sensor_name=power_sensor_name,
    )
    tz = pytz.timezone("Europe/Amsterdam")

    # transition from cheap to expensive (90 -> 100)
    start = tz.localize(datetime(2015, 1, 2, 14, 0, 0))
    end = tz.localize(datetime(2015, 1, 2, 16, 0, 0))
    resolution = timedelta(minutes=15)

    flex_model = {
        "soc-min": soc_min,
        "soc-max": soc_max,
        "soc-at-start": soc_at_start,
        "soc-unit": soc_unit,
        "roundtrip-efficiency": 1,
        "storage-efficiency": 1,
        "power-capacity": "1 MW",
    }

    scheduler: Scheduler = StorageScheduler(
        battery,
        start,
        end,
        resolution,
        flex_model=flex_model,
        flex_context={
            "site-power-capacity": "1 MW",
        },
    )
    schedule = scheduler.compute()

    if power_sensor_name == "power (kW)":
        schedule /= 1000

    # Check if constraints were met
    if isinstance(soc_at_start, str):
        soc_at_start = ur.Quantity(soc_at_start).to("MWh").magnitude
    elif isinstance(soc_at_start, float) or isinstance(soc_at_start, int):
        soc_at_start = soc_at_start * convert_units(1, soc_unit, "MWh")
    check_constraints(battery, schedule, soc_at_start)

    # charge fully in the cheap price period (100 kWh -> 1000kWh)
    assert schedule[:4].sum() * 0.25 == 0.9

    # discharge fully in the expensive price period (1000 kWh -> 100 kWh)
    assert schedule[4:].sum() * 0.25 == -0.9


@pytest.mark.parametrize(
    "ts_field, ts_specs",
    [
        # The battery only has time to charge up to 950 kWh halfway
        (
            "power-capacity",
            [
                {
                    "start": "2015-01-02T15:00+01",
                    "end": "2015-01-02T17:00+01",
                    "value": "850 kW",
                }
            ],
        ),
        # Same, but the event time is specified with a duration instead of an end time
        (
            "power-capacity",
            [
                {
                    "start": "2015-01-02T15:00+01",
                    "duration": "PT2H",
                    "value": "850 kW",
                }
            ],
        ),
        # Can only charge up to 950 kWh halfway
        (
            "soc-maxima",
            [
                {
                    "datetime": "2015-01-02T16:00+01",
                    "value": "950 kWh",
                }
            ],
        ),
        # Must end up at a maximum of 200 kWh, for which it is cheapest to charge to 950 and then to discharge to 200
        (
            "soc-maxima",
            [
                {
                    "start": "2015-01-02T16:45+01",
                    "duration": "PT15M",
                    "value": "200 kWh",
                }
            ],
        ),
    ],
)
def test_battery_storage_with_time_series_in_flex_model(
    add_battery_assets,
    db,
    ts_field,
    ts_specs,
):
    """
    Test scheduling a 1 MWh battery for 2h with a low -> high price transition with
    a time series used for the various flex-model fields.
    """

    soc_min = "100 kWh"
    soc_max = "1 MWh"
    soc_at_start = "100 kWh"

    epex_da, battery = get_sensors_from_db(
        db,
        add_battery_assets,
        battery_name="Test battery",
        power_sensor_name="power",
    )
    tz = pytz.timezone("Europe/Amsterdam")

    # transition from cheap to expensive (90 -> 100)
    start = tz.localize(datetime(2015, 1, 2, 15, 0, 0))
    end = tz.localize(datetime(2015, 1, 2, 17, 0, 0))
    resolution = timedelta(minutes=15)

    flex_model = {
        "soc-min": soc_min,
        "soc-max": soc_max,
        "soc-at-start": soc_at_start,
        "roundtrip-efficiency": 1,
        "storage-efficiency": 1,
        "power-capacity": "1 MW",
    }
    flex_model[ts_field] = ts_specs

    scheduler: Scheduler = StorageScheduler(
        battery,
        start,
        end,
        resolution,
        flex_model=flex_model,
        flex_context={
            "site-power-capacity": "1 MW",
        },
    )
    schedule = scheduler.compute()

    # Check if constraints were met
    soc_at_start = ur.Quantity(soc_at_start).to("MWh").magnitude
    check_constraints(battery, schedule, soc_at_start)

    # charge 850 kWh in the cheap price period (100 kWh -> 950kWh)
    assert schedule[:4].sum() * 0.25 == pytest.approx(0.85)

    # discharge fully or to what's needed in the expensive price period (950 kWh -> 100 or 200 kWh)
    if ts_field == "soc-minima":
        assert schedule[4:].sum() * 0.25 == pytest.approx(-0.75)
    else:
        assert schedule[4:].sum() * 0.25 == pytest.approx(-0.85)


def test_unavoidable_capacity_breach():
    """Check our ability to limit a capacity breach.

    We want to check two behaviours:
    1) The breach should be as small as possible (i.e. flattened from the top).
    2) The breach should not be repeated if that can be avoided (i.e. no taking advantage of created slacks).
    """
    start = pd.Timestamp("2020-01-01T00:00:00+01")
    end = pd.Timestamp("2020-01-02T00:00:00+01")
    resolution = timedelta(hours=1)
    soc_at_start = 0.4
    soc_max = 1
    soc_min = 0
    power_capacity = 0.1

    # This target means we must breach the consumption capacity (from 0.1 to 0.2)
    soc_target = 0.6
    soc_target_datetime = pd.Timestamp("2020-01-01T01:00:00+01")

    device_constraints = [
        initialize_df(StorageScheduler.COLUMNS, start, end, resolution)
    ]
    ems_constraints = initialize_df(StorageScheduler.COLUMNS, start, end, resolution)
    empty_commitment = initialize_df(
        [
            "quantity",
            "downwards deviation price",
            "upwards deviation price",
            "group",
        ],
        start,
        end,
        resolution,
    )
    commitments = []
    commitments.append(empty_commitment.copy())

    device_constraints[0]["max"] = soc_max - soc_at_start
    device_constraints[0]["min"] = soc_min - soc_at_start
    device_constraints[0]["derivative max"] = 1
    device_constraints[0]["derivative min"] = -1

    slope = np.arange(0, len(commitments[0])) / (len(commitments[0]) - 1)

    # Introduce an amazing chance to consume without costs
    slope[10] = -100

    # Day Ahead market commitment
    commitments[0]["quantity"] = 0
    commitments[0]["downwards deviation price"] = 90 + slope
    commitments[0]["upwards deviation price"] = 100 + slope
    # `len(commitments[0])` is the number of time slots. Each slot is part of a unique group.
    commitments[0]["group"] = list(range(len(commitments[0])))

    # Consumption Capacity Breach Commitment (1 group, so penalized once)
    commitments.append(empty_commitment.copy())
    commitments[-1]["quantity"] = power_capacity
    # positive price because breaching in the upwards (consumption) direction is penalized
    commitments[-1]["upwards deviation price"] = 1000
    # todo: also allow None values to model a one-sided commitment
    commitments[-1]["downwards deviation price"] = np.nan
    commitments[-1]["group"] = 1

    def run_scheduler(device_constraints):
        _, _, results, model = device_scheduler(
            device_constraints=device_constraints,
            ems_constraints=ems_constraints,
            commitments=commitments,
            initial_stock=soc_at_start,
        )
        assert results.solver.termination_condition == "optimal"

        schedule = initialize_series(
            data=[model.ems_power[0, j].value for j in model.j],
            start=start,
            end=end,
            resolution=resolution,
        )
        print(schedule)
        return schedule, results

    schedule, results = run_scheduler(device_constraints)

    # Discharge the whole battery, right at the end
    assert np.isclose(sum(schedule), -0.4)
    assert np.isclose(schedule.values[-1], -0.5)
    # Take advantage of the free consumption
    assert np.isclose(schedule.values[10], 0.1)
    # Do nothing otherwise
    assert all(np.isclose(schedule.values[:10], 0))
    assert all(np.isclose(schedule.values[11:-1], 0))

    targets = initialize_series(None, start, end, resolution)
    targets[soc_target_datetime] = soc_target
    # device_constraints[0]["equals"] = targets - soc_at_start

    # Convert SoC datetimes to periods with a start and end.
    soc_values = [{"datetime": soc_target_datetime, "value": soc_target}]
    for soc in soc_values:
        TimedEventSchema().check_time_window(soc)
    device_constraints[0]["equals"] = build_device_soc_values(
        soc_values=soc_values,
        soc_at_start=soc_at_start,
        start_of_schedule=start,
        end_of_schedule=end,
        resolution=resolution,
    )

    schedule, results = run_scheduler(device_constraints)

    # Discharge the whole battery
    assert np.isclose(max(schedule), 0.2)  # this is the breach
    # the breach should happen twice:
    # - once for reaching the SoC target, and
    # - once for exploiting the slack that was created for reaching the SoC target
    assert len(schedule[np.isclose(schedule, 0.2)]) == 2
    assert np.isclose(sum(schedule), -0.4)
    # Reach the target (from 0.4 to 0.6)
    assert np.isclose(schedule.values[0], 0.2)
    # Take (too much) advantage of the free consumption
    assert np.isclose(schedule.values[10], 0.2)
    # Do nothing otherwise
    assert all(np.isclose(schedule.values[1:10], 0))
    assert all(np.isclose(schedule.values[11:-1], 0))

    # Consumption Capacity Breach Commitment (n groups, so penalized with every breach)
    commitments.append(empty_commitment.copy())
    commitments[-1]["quantity"] = power_capacity
    # positive price because breaching in the upwards (consumption) direction is penalized
    commitments[-1]["upwards deviation price"] = 1000
    # todo: also allow None values to model a one-sided commitment
    commitments[-1]["downwards deviation price"] = np.nan
    commitments[-1]["group"] = list(range(len(commitments[0])))

    schedule, results = run_scheduler(device_constraints)

    # Discharge the whole battery
    assert np.isclose(max(schedule), 0.2)  # this is the breach
    # the breach should now happen only once, for reaching the SoC target
    assert len(schedule[np.isclose(schedule, 0.2)]) == 1
    assert np.isclose(sum(schedule), -0.4)
    # Reach the target (from 0.4 to 0.6)
    assert np.isclose(schedule.values[0], 0.2)
    # Take advantage of the free consumption
    assert np.isclose(schedule.values[10], 0.1)
    # Do nothing otherwise
    assert all(np.isclose(schedule.values[1:10], 0))
    assert all(np.isclose(schedule.values[11:-1], 0))


def test_multiple_commitments_per_group():
    """Check draining a battery while expanding the number of commitments:

    1) against increasing prices -> discharge all in the last step
    2) also with a limited capacity -> discharge as late as possible
    3) also with peak pricing -> discharge at a constant rate
    """
    start = pd.Timestamp("2020-01-01T00:00:00")
    end = pd.Timestamp("2020-01-02T00:00:00")
    resolution = timedelta(hours=1)
    soc_at_start = 0.4
    soc_max = 1
    soc_min = 0

    device_constraints = [
        initialize_df(StorageScheduler.COLUMNS, start, end, resolution)
    ]
    ems_constraints = initialize_df(StorageScheduler.COLUMNS, start, end, resolution)
    empty_commitment = initialize_df(
        [
            "quantity",
            "downwards deviation price",
            "upwards deviation price",
            "group",
        ],
        start,
        end,
        resolution,
    )
    commitments = []
    commitments.append(empty_commitment.copy())

    device_constraints[0]["max"] = soc_max - soc_at_start
    device_constraints[0]["min"] = soc_min - soc_at_start
    device_constraints[0]["derivative max"] = 1
    device_constraints[0]["derivative min"] = -1

    slope = np.arange(0, len(commitments[0])) / (len(commitments[0]) - 1)

    # Day Ahead market commitment
    commitments[0]["quantity"] = 0
    commitments[0]["downwards deviation price"] = 90 + slope
    commitments[0]["upwards deviation price"] = 100 + slope
    commitments[0]["group"] = list(range(len(commitments[0])))

    def run_scheduler():
        _, _, results, model = device_scheduler(
            device_constraints=device_constraints,
            ems_constraints=ems_constraints,
            commitments=commitments,
            initial_stock=soc_at_start,
        )
        assert results.solver.termination_condition == "optimal"

        schedule = initialize_series(
            data=[model.ems_power[0, j].value for j in model.j],
            start=start,
            end=end,
            resolution=resolution,
        )
        print(schedule)
        costs = value(model.costs)
        commitment_costs = model.commitment_costs
        return schedule, results, costs, commitment_costs

    schedule, results, costs, commitment_costs = run_scheduler()

    # Discharge the whole battery
    assert np.isclose(sum(schedule), -0.4)
    assert np.isclose(schedule.values[-1], -0.4)

    # Check costs
    assert costs == -36.4

    # Production Capacity Breach Commitment
    commitments.append(empty_commitment.copy())
    commitments[-1]["quantity"] = -0.1
    # negative price because breaching in the downwards (production) direction is penalized
    commitments[-1]["downwards deviation price"] = -1000
    # positive price because breaching in the upwards (consumption) direction is penalized
    # todo: also allow None values to model a one-sided commitment
    commitments[-1]["upwards deviation price"] = np.nan
    commitments[-1]["group"] = 1

    schedule, results, costs, commitment_costs = run_scheduler()

    # Discharge the whole battery
    assert np.isclose(sum(schedule), -0.4)
    assert all(np.isclose(schedule.values[-4:], [-0.1, -0.1, -0.1, -0.1]))

    # Check costs
    assert costs == (commitments[0]["downwards deviation price"][-4:] * -0.1).sum()

    # Peak Power Commitment
    commitments.append(empty_commitment.copy())
    commitments[-1]["quantity"] = 0
    # negative price because peaking in the downwards (production) direction is penalized
    commitments[-1]["downwards deviation price"] = -80
    # positive price because breaching in the upwards (consumption) direction is penalized
    commitments[-1]["upwards deviation price"] = 80
    commitments[-1]["group"] = 1

    schedule, results, costs, commitment_costs = run_scheduler()

    # Discharge the whole battery
    assert np.isclose(sum(schedule), -0.4)
    assert all(np.isclose(schedule.values, [-0.4 / len(schedule)] * len(schedule)))

    # Check costs
    cost_of_energy = (
        commitments[0]["downwards deviation price"] * (-0.4 / len(schedule))
    ).sum()
    cost_of_energy_peak = -80 * -0.4 / len(schedule)
    expected_cost = cost_of_energy + cost_of_energy_peak
    assert costs == pytest.approx(expected_cost)

    assert len(commitment_costs) == len(commitments)
    assert sum(commitment_costs.values()) == pytest.approx(expected_cost)


def test_multiple_devices_simultaneous_scheduler():
    """
    Test the device_scheduler for simultaneously scheduling multiple devices (2):
    - Schedules are created for each device.
    - Costs are calculated accurately.

    Test cases:
    1. All devices will reach target SOC
    2. Lower EMS capacity, some unmet demand
    """
    # Test configuration
    start = pd.Timestamp("2020-01-01T00:00:00")
    end = pd.Timestamp("2020-01-02T00:00:00")
    resolution = timedelta(hours=1)
    num_devices = 2

    # Device parameters
    soc_at_start = [0] * num_devices
    soc_max, soc_min = [1] * num_devices, [0] * num_devices
    start_datetime = ["2020-01-01 01:00:00"] * num_devices
    target_datetime = ["2020-01-01 06:00:00", "2020-01-01 03:00:00"]
    target_value = [1] * num_devices

    market_prices = [
        0.8598,
        1.4613,  # cheap from 01:00 to 02:00
        2430.3887,
        3000.1779,
        18.6619,  # cheap from 04:00 to 0:500
        369.3274,
        169.8719,
        174.2279,
        174.2279,
        174.2279,
        175.4258,
        1.5697,
        174.2763,
        174.2279,
        175.2564,
        202.6992,
        218.4413,
        229.9242,
        295.1069,
        240.7174,
        249.2479,
        238.2732,
        229.8395,
        216.5779,
    ]
    soc_target_penalty = 10000

    def initialize_combined_constraints(
        num_devices: int, max_derivative: float = 1, min_derivative: float = 0
    ):
        device_constraints = []
        for d in range(num_devices):
            constraints = initialize_df(
                StorageScheduler.COLUMNS, start, end, resolution
            )
            start_time = pd.Timestamp(start_datetime[d]) - timedelta(hours=1)
            constraints["max"] = soc_max[d] - soc_at_start[d]
            constraints["min"] = soc_min[d] - soc_at_start[d]
            constraints["derivative max"] = max_derivative
            constraints["derivative min"] = min_derivative
            constraints.loc[
                :start_time, ["max", "min", "derivative max", "derivative min"]
            ] = 0
            device_constraints.append(constraints)

        return device_constraints

    def initialize_combined_commitments(num_devices: int):
        commitments = []

        # Model energy contract for the site
        energy_commitment = initialize_energy_commitment(
            start=start,
            end=end,
            resolution=resolution,
            market_prices=market_prices,
        )
        commitments.append(energy_commitment)

        # Model penalties for demand unmet per device
        for d in range(num_devices):
            device_commitment = initialize_device_commitment(
                start=start,
                end=end,
                resolution=resolution,
                device=d,
                target_datetime=target_datetime[d],
                target_value=target_value[d],
                soc_at_start=soc_at_start[d],
                soc_target_penalty=soc_target_penalty,
            )
            commitments.append(device_commitment)

        return commitments

    ems_constraints = initialize_df(StorageScheduler.COLUMNS, start, end, resolution)
    ems_constraints["derivative max"] = 1
    ems_constraints["derivative min"] = 0

    device_constraints = initialize_combined_constraints(num_devices)
    commitments = initialize_combined_commitments(num_devices)

    # Test case 1: No unmet demand
    initial_stocks = soc_at_start
    _, _, results, model = device_scheduler(
        device_constraints=device_constraints,
        ems_constraints=ems_constraints,
        commitments=commitments,
        initial_stock=initial_stocks,
    )
    assert results.solver.termination_condition == "optimal"

    schedules = [
        initialize_series(
            data=[model.ems_power[d, j].value for j in model.j],
            start=start,
            end=end,
            resolution=resolution,
        )
        for d in range(num_devices)
    ]

    individual_costs = [
        (d, sum(schedule[j] * market_prices[j] for j in range(len(market_prices))))
        for d, schedule in enumerate(schedules)
    ]

    # Expected results with no unmet demand
    expected_schedules = [
        [0] * 4
        + [1]
        + [0] * 19,  # the first EV leaves later, and takes the second-cheapest slot
        [0, 1] + [0] * 22,  # the second EV leaves earlier, and gets the cheapest slot
    ]
    total_expected_demand = np.array(expected_schedules).sum()
    expected_individual_costs = [(0, 18.66), (1, 1.46)]

    # Assertions
    assert all(
        np.isclose(schedule, expected_schedules[d]).all()
        for d, schedule in enumerate(schedules)
    ), "Schedules mismatch: Device schedules do not match the expected schedules."

    assert all(
        device == d and pytest.approx(cost, 0.01) == expected_individual_costs[d][1]
        for d, (device, cost) in enumerate(individual_costs)
    ), "Individual costs mismatch: Costs for one or more devices are not calculated as expected."

    # Test case 2: With lower EMS capacity and unmet demand
    ems_constraints = initialize_df(StorageScheduler.COLUMNS, start, end, resolution)
    ems_constraints["derivative max"] = 0.25
    ems_constraints["derivative min"] = 0

    device_constraints = initialize_combined_constraints(
        num_devices,
        max_derivative=0.25,
        min_derivative=0,  # todo: change does not seem required
    )
    _, _, results, model = device_scheduler(
        device_constraints=device_constraints,
        ems_constraints=ems_constraints,
        commitments=commitments,
        initial_stock=initial_stocks,
    )
    assert results.solver.termination_condition == "optimal"

    schedules = [
        initialize_series(
            data=[model.ems_power[d, j].value for j in model.j],
            start=start,
            end=end,
            resolution=resolution,
        )
        for d in range(num_devices)
    ]

    individual_costs = [
        (d, sum(schedule[j] * market_prices[j] for j in range(len(market_prices))))
        for d, schedule in enumerate(schedules)
    ]

    # Expected results with unfair unmet demand and unfair costs
    expected_schedules = [
        [0, 0.25, 0, 0, 0.25, 0.25, 0.25]
        + [0] * 17,  # the first EV leaves later, and takes the four cheapest slots
        [0, 0, 0.25, 0.25]
        + [0]
        * 20,  # the second EV leaves earlier, and takes the remaining (expensive) slots
    ]
    total_expected_demand_unmet = (
        total_expected_demand - np.array(expected_schedules).sum()
    )
    assert total_expected_demand_unmet > 0
    expected_individual_costs = [(0, 139.83), (1, 1357.64)]

    # Assertions
    assert all(
        np.isclose(schedule, expected_schedules[d]).all()
        for d, schedule in enumerate(schedules)
    ), "Schedules mismatch: Device schedules do not match the expected schedules."

    assert all(
        device == d and pytest.approx(cost, 0.01) == expected_individual_costs[d][1]
        for d, (device, cost) in enumerate(individual_costs)
    ), "Individual costs mismatch: Costs for one or more devices are not calculated as expected."


def test_multiple_devices_sequential_scheduler():
    start = pd.Timestamp("2023-01-01T00:00:00")
    end = pd.Timestamp("2023-01-02T00:00:00")
    resolution = timedelta(hours=1)

    soc_at_start = [0] * 2
    soc_max = [1] * 2
    soc_min = [0] * 2

    start_datetime = ["2023-01-01 01:00:00"] * 2
    target_datetime = [
        "2023-01-01 06:00:00",
        "2023-01-01 03:00:00",
    ]  # todo: problem with interpreting datetime of soc-target?
    target_value = [1] * 2

    market_prices = [
        0.8598,
        1.4613,  # cheap from 01:00 to 02:00
        2430.3887,
        3000.1779,
        18.6619,  # cheap from 04:00 to 0:500
        369.3274,
        169.8719,
        174.2279,
        174.2279,
        174.2279,
        175.4258,
        1.5697,
        174.2763,
        174.2279,
        175.2564,
        202.6992,
        218.4413,
        229.9242,
        295.1069,
        240.7174,
        249.2479,
        238.2732,
        229.8395,
        216.5779,
    ]
    soc_target_penalty = 10000

    def initialize_device_constraints(
        num_devices: int,
        soc_at_start: list[float],
        soc_max: list[float],
        soc_min: list[float],
        target_datetime: list[str],
        target_value: list[float],
        start_datetime: list[str],
    ):
        device_constraints = []
        for d in range(num_devices):
            constraints = initialize_df(
                StorageScheduler.COLUMNS, start, end, resolution
            )

            start_time = pd.Timestamp(start_datetime[d]) - timedelta(hours=1)

            constraints["max"] = soc_max[d] - soc_at_start[d]
            constraints["min"] = soc_min[d] - soc_at_start[d]
            constraints["derivative max"] = 1
            constraints["derivative min"] = 0
            # constraints.loc[target_datetime[d], "min"] = (
            #     target_value[d] - soc_at_start[d]
            # )
            constraints.loc[
                :start_time, ["max", "min", "derivative max", "derivative min"]
            ] = 0
            device_constraints.append(constraints)
        return device_constraints

    def initialize_ems_constraints():
        ems_constraints = initialize_df(
            StorageScheduler.COLUMNS, start, end, resolution
        )
        ems_constraints["derivative max"] = 1
        ems_constraints["derivative min"] = 0
        return ems_constraints

    def run_sequential_scheduler():
        num_devices = len(soc_at_start)

        device_constraints = initialize_device_constraints(
            num_devices=num_devices,
            soc_at_start=soc_at_start,
            soc_max=soc_max,
            soc_min=soc_min,
            target_datetime=target_datetime,
            target_value=target_value,
            start_datetime=start_datetime,
        )

        # Model energy contract for the site
        energy_commitment = initialize_energy_commitment(
            start=start,
            end=end,
            resolution=resolution,
            market_prices=market_prices,
        )

        ems_constraints = initialize_ems_constraints()

        all_schedules = []
        total_costs = []
        combined_schedule = [0] * len(market_prices)
        unmet_targets = []

        for d in range(num_devices):
            initial_stock = soc_at_start[d]

            # Model penalties for demand unmet per device
            device_commitment = initialize_device_commitment(
                start=start,
                end=end,
                resolution=resolution,
                device=0,
                target_datetime=target_datetime[d],
                target_value=target_value[d],
                soc_at_start=soc_at_start[d],
                soc_target_penalty=soc_target_penalty,
            )

            # Compute the schedule for device d
            _, _, results, model = device_scheduler(
                device_constraints=[device_constraints[d]],
                ems_constraints=ems_constraints,
                commitments=[energy_commitment, device_commitment],
                initial_stock=initial_stock,
            )
            assert results.solver.termination_condition == "optimal"

            schedule = initialize_series(
                data=[model.ems_power[0, j].value for j in model.j],
                start=start,
                end=end,
                resolution=resolution,
            )
            all_schedules.append(schedule)

            for j in range(len(schedule)):
                combined_schedule[j] += schedule[j]

            # Determine new headroom
            ems_constraints["derivative max"] -= schedule
            ems_constraints["derivative min"] -= schedule

            total_cost = sum(
                schedule[j] * market_prices[j] for j in range(len(market_prices))
            )
            total_costs.append(total_cost)

            final_soc = initial_stock + sum(schedule)
            if final_soc < target_value[d]:
                unmet_targets.append((d, final_soc))

        return (
            all_schedules,
            total_costs,
            sum(total_costs),
            combined_schedule,
            unmet_targets,
        )

    schedules, costs, total_cost_all_devices, combined_schedule, unmet_targets = (
        run_sequential_scheduler()
    )

    expected_schedules = [
        [0, 1] + [0] * 22,  # the first EV leaves later, but takes the cheapest slot
        [0, 0, 1]
        + [0]
        * 21,  # the second EV leaves earlier, and gets the only (expensive) slot left
    ]
    expected_costs = [(0, 1.46), (1, 2430.39)]

    assert all(
        np.isclose(schedules[d], expected_schedules[d]).all()
        for d in range(len(schedules))
    ), "Schedules do not match expected values."

    assert all(
        pytest.approx(costs[d], 0.01) == expected_costs[d][1] for d in range(len(costs))
    ), "Costs do not match expected values."

    assert total_cost_all_devices == sum(
        expected_cost[1] for expected_cost in expected_costs
    ), "Total cost mismatch."
