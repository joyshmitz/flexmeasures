from __future__ import annotations

from datetime import datetime, timedelta

from humanize.time import naturaldelta

from flexmeasures.data.models.time_series import TimedBelief


import sqlalchemy as sa

from flexmeasures import Sensor, Account
from flexmeasures.data.models.generic_assets import GenericAsset
from flexmeasures.data.schemas.reporting import StatusSchema


def get_sensors(
    account: Account | list[Account] | None,
    include_public_assets: bool = False,
    sensor_id_allowlist: list[int] | None = None,
    sensor_name_allowlist: list[str] | None = None,
) -> list[Sensor]:
    """Return a list of Sensor objects that belong to the given account, and/or public sensors.

    :param account:                 select only sensors from this account (or list of accounts)
    :param include_public_assets:   if True, include sensors that belong to a public asset
    :param sensor_id_allowlist:     optionally, allow only sensors whose id is in this list
    :param sensor_name_allowlist:   optionally, allow only sensors whose name is in this list
    """
    sensor_query = Sensor.query
    if account is None:
        account_ids = []
    elif isinstance(account, list):
        account_ids = [account.id for account in account]
    else:
        account_ids = [account.id]
    sensor_query = sensor_query.join(GenericAsset).filter(
        Sensor.generic_asset_id == GenericAsset.id
    )
    if include_public_assets:
        sensor_query = sensor_query.filter(
            sa.or_(
                GenericAsset.account_id.in_(account_ids),
                GenericAsset.account_id.is_(None),
            )
        )
    else:
        sensor_query = sensor_query.filter(GenericAsset.account_id.in_(account_ids))
    if sensor_id_allowlist:
        sensor_query = sensor_query.filter(Sensor.id.in_(sensor_id_allowlist))
    if sensor_name_allowlist:
        sensor_query = sensor_query.filter(Sensor.name.in_(sensor_name_allowlist))
    return sensor_query.all()


def get_most_recent_knowledge_time(
    sensor: Sensor, staleness_search: dict
) -> datetime | None:
    """Get the knowledge time of the sensor's most recent event.

    This knowledge time represents when you could have known about the event
    (specifically, when you could have formed an ex-post belief about it).
    """
    staleness_bdf = TimedBelief.search(
        sensors=sensor,
        most_recent_events_only=True,
        **staleness_search,
    )
    if staleness_bdf.empty:
        return None
    return staleness_bdf.knowledge_times[-1]


def get_staleness(
    sensor: Sensor, staleness_search: dict, now: datetime
) -> timedelta | None:
    """Get the staleness of the sensor.

    :param sensor:              The sensor to compute the staleness for.
    :param staleness_search:    Deserialized keyword arguments to `TimedBelief.search`.
    :param now:                 Datetime representing now, used both to mask future beliefs,
                                and to measures staleness against.
    """

    # Mask beliefs before now
    staleness_search = staleness_search.copy()  # no inplace operations
    beliefs_before = staleness_search.get("beliefs_before")
    if beliefs_before is not None:
        staleness_search["beliefs_before"] = min(beliefs_before, now)
    else:
        staleness_search["beliefs_before"] = now

    most_recent_knowledge_time = get_most_recent_knowledge_time(
        sensor=sensor, staleness_search=staleness_search
    )
    if most_recent_knowledge_time is not None:
        staleness = now - most_recent_knowledge_time
    else:
        staleness = None

    return staleness


def get_status_specs(sensor: Sensor) -> dict:
    """Get status specs from a given sensor."""

    # Check for explicitly defined status specs
    status_specs = sensor.attributes.get("status_specs")
    if status_specs is None:
        # Default to status specs for economical sensors with daily updates
        if sensor.knowledge_horizon_fnc == "x_days_ago_at_y_oclock":
            status_specs = {"staleness_search": {}, "max_staleness": f"P1D"}
        else:
            # Default to status specs indicating immediate staleness after knowledge time
            status_specs = {"staleness_search": {}, "max_staleness": "PT0H"}
    return status_specs


def get_status(
    sensor: Sensor,
    now: datetime,
    status_specs: dict | None = None,
) -> dict:
    """Get the status of the sensor"""
    if status_specs is None:
        status_specs = get_status_specs(sensor=sensor)
    status_specs = StatusSchema().load(status_specs)
    max_staleness = status_specs.pop("max_staleness")
    staleness_search = status_specs.pop("staleness_search")
    staleness = get_staleness(sensor=sensor, staleness_search=staleness_search, now=now)
    if staleness is not None:
        staleness_since = now - staleness
        stale = staleness > max_staleness
    else:
        staleness_since = None
        stale = True
    status = dict(
        staleness=staleness,
        stale=stale,
        staleness_since=staleness_since,
        reason=("" if stale else "not ")
        + f"more than {naturaldelta(max_staleness)} old",
    )
    return status
