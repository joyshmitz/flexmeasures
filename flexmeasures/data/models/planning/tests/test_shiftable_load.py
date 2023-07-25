from datetime import datetime, timedelta
import pytest
import pytz

from flexmeasures.data.models.time_series import Sensor
from flexmeasures.data.models.planning.shiftable_load import ShiftableLoadScheduler


tz = pytz.timezone("Europe/Amsterdam")
start = tz.localize(datetime(2015, 1, 2))
end = tz.localize(datetime(2015, 1, 3))
resolution = timedelta(hours=1)


@pytest.mark.parametrize(
    "load_type, optimal_start",
    [("INFLEXIBLE", datetime(2015, 1, 2, 0)), ("SHIFTABLE", datetime(2015, 1, 2, 8))],
)
def test_shiftable_scheduler(
    add_battery_assets, shiftable_load, load_type, optimal_start
):
    """
    Test scheduling a load of 4kW of power that last 4h using the ShiftableLoadScheduler
    without time restrictions.
    """

    # get the sensors from the database
    epex_da = Sensor.query.filter(Sensor.name == "epex_da").one_or_none()

    flex_model = {
        "consumption-price-sensor": epex_da.id,
        "duration": "PT4H",
        "load-type": load_type,
        "power": 4,
    }

    scheduler = ShiftableLoadScheduler(
        shiftable_load,
        start,
        end,
        resolution,
        flex_model=flex_model,
    )
    schedule = scheduler.compute()

    optimal_start = tz.localize(optimal_start)

    mask = (optimal_start <= schedule.index) & (
        schedule.index < optimal_start + timedelta(hours=4)
    )

    assert (schedule[mask] == 4).all()
    assert (schedule[~mask] == 0).all()


@pytest.mark.parametrize(
    "load_type, optimal_start",
    [("INFLEXIBLE", datetime(2015, 1, 2, 0)), ("SHIFTABLE", datetime(2015, 1, 2, 8))],
)
def test_duration_exceeds_planning_window(
    add_battery_assets, shiftable_load, load_type, optimal_start
):
    """
    Test scheduling a load that last longer than the planning window.
    """

    # get the sensors from the database
    epex_da = Sensor.query.filter(Sensor.name == "epex_da").one_or_none()

    flex_model = {
        "consumption-price-sensor": epex_da.id,
        "duration": "PT48H",
        "load-type": load_type,
        "power": 4,
    }

    scheduler = ShiftableLoadScheduler(
        shiftable_load,
        start,
        end,
        resolution,
        flex_model=flex_model,
    )
    schedule = scheduler.compute()

    optimal_start = tz.localize(optimal_start)

    assert (schedule == 4).all()


def test_shiftable_scheduler_time_restrictions(add_battery_assets, shiftable_load):
    """
    Test ShiftableLoadScheduler with a time restrictions consisting of a block of 2h starting
    at 8am. The resulting schedules avoid the 8am-10am period and schedules for a valid period.
    """

    # get the sensors from the database
    epex_da = Sensor.query.filter(Sensor.name == "epex_da").one_or_none()

    # time parameters

    flex_model = {
        "consumption-price-sensor": epex_da.id,
        "duration": "PT4H",
        "load-type": "SHIFTABLE",
        "power": 4,
        "time-restrictions": [
            {"start": "2015-01-02T08:00:00+01:00", "duration": "PT2H"}
        ],
    }

    scheduler = ShiftableLoadScheduler(
        shiftable_load,
        start,
        end,
        resolution,
        flex_model=flex_model,
    )
    schedule = scheduler.compute()

    optimal_start = tz.localize(datetime(2015, 1, 2, 10))

    mask = (optimal_start <= schedule.index) & (
        schedule.index < optimal_start + timedelta(hours=4)
    )

    assert (schedule[mask] == 4).all()
    assert (schedule[~mask] == 0).all()

    # check that the time restrictions are fulfilled
    time_restrictions = scheduler.flex_model["time_restrictions"]
    time_restrictions = time_restrictions.tz_convert(tz)

    assert (schedule[time_restrictions] == 0).all()


def test_breakable_scheduler_time_restrictions(add_battery_assets, shiftable_load):
    """
    Test breakable load_type of ShiftableLoadScheduler by introducing four 1-hour restrictions
    interspaced by 1 hour. The equivalent mask would be the following: [0,...,0,1,0,1,0,1,0,1,0, ...,0].
    Trying to get the best prices (between 9am and 4pm), his makes the schedule choose time periods between
    the time restrictions.
    """

    # get the sensors from the database
    epex_da = Sensor.query.filter(Sensor.name == "epex_da").one_or_none()

    # time parameters

    flex_model = {
        "consumption-price-sensor": epex_da.id,
        "duration": "PT4H",
        "load-type": "BREAKABLE",
        "power": 4,
        "time-restrictions": [
            {"start": "2015-01-02T09:00:00+01:00", "duration": "PT1H"},
            {"start": "2015-01-02T11:00:00+01:00", "duration": "PT1H"},
            {"start": "2015-01-02T13:00:00+01:00", "duration": "PT1H"},
            {"start": "2015-01-02T15:00:00+01:00", "duration": "PT1H"},
        ],
    }

    scheduler = ShiftableLoadScheduler(
        shiftable_load,
        start,
        end,
        resolution,
        flex_model=flex_model,
    )
    schedule = scheduler.compute()

    expected_schedule = [0] * 8 + [4, 0, 4, 0, 4, 0, 4, 0] + [0] * 8

    assert (schedule == expected_schedule).all()

    # check that the time restrictions are fulfilled
    time_restrictions = scheduler.flex_model["time_restrictions"]
    time_restrictions = time_restrictions.tz_convert(tz)

    assert (schedule[time_restrictions] == 0).all()


@pytest.mark.parametrize(
    "load_type, time_restrictions",
    [
        ("BREAKABLE", [{"start": "2015-01-02T00:00:00+01:00", "duration": "PT24H"}]),
        ("INFLEXIBLE", [{"start": "2015-01-02T03:00:00+01:00", "duration": "PT21H"}]),
        ("SHIFTABLE", [{"start": "2015-01-02T03:00:00+01:00", "duration": "PT21H"}]),
    ],
)
def test_impossible_schedules(
    add_battery_assets, shiftable_load, load_type, time_restrictions
):
    """
    Test schedules with time restrictions that make a 4h block not fit anytime during the
    planned window.
    """

    # get the sensors from the database
    epex_da = Sensor.query.filter(Sensor.name == "epex_da").one_or_none()

    flex_model = {
        "consumption-price-sensor": epex_da.id,
        "duration": "PT4H",
        "load-type": load_type,
        "power": 4,
        "time-restrictions": time_restrictions,
    }

    scheduler = ShiftableLoadScheduler(
        shiftable_load,
        start,
        end,
        resolution,
        flex_model=flex_model,
    )

    with pytest.raises(ValueError):
        scheduler.compute()
