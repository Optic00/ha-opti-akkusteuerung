"""Real local HA Recorder database, synthetic measurements only."""

from datetime import timedelta
import pytest
from custom_components.opti_akku.demand_history import read_recorder
from tests.test_demand import NOW


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url):
    """Initialize database fixture before the autouse HA fixture."""


async def test_real_recorder_database_statistics_units(recorder_mock, hass):
    """Exercise HA 2026.9 Recorder APIs and unit conversion, not reader mocks."""
    from functools import partial
    from homeassistant.components.recorder.statistics import async_import_statistics
    from homeassistant.components.recorder.models import StatisticMeanType
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    metadata = {
        "statistic_id": "sensor.historical_house",
        "source": "recorder",
        "name": "Synthetic house",
        "has_sum": False,
        "mean_type": StatisticMeanType.ARITHMETIC,
        "unit_of_measurement": "kW",
        "unit_class": "power",
    }
    async_import_statistics(hass, metadata, [{"start": NOW, "mean": 0.65, "min": 0.4, "max": 1.2}])
    await async_wait_recording_done(hass)
    result = await recorder_mock.async_add_executor_job(
        partial(
            read_recorder, hass, {"house": "sensor.historical_house", "summer_mode": "binary_sensor.summer"}, NOW, NOW + timedelta(hours=1)
        )
    )
    assert result[NOW.isoformat()]["house_w"] == 650
    assert result[NOW.isoformat()]["summer_mode"] is None
