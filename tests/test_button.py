"""Direct button entity delegation."""

from unittest.mock import AsyncMock, Mock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.button import (
    OptiComparisonStart,
    OptiComparisonStop,
    OptiDemandHistoryImport,
)


async def test_standard_journal_buttons_delegate_to_coordinator():
    coordinator = Mock(
        data={}, shadow_mode=False, last_update_success=True,
        async_add_listener=Mock(return_value=Mock()),
        async_start_comparison=AsyncMock(), async_stop_comparison=AsyncMock(),
        async_import_demand_history=AsyncMock(),
    )
    entry = MockConfigEntry(domain="opti_akku", data={"profile": "sma_stp_se"})
    entry.runtime_data = coordinator

    await OptiComparisonStart(entry).async_press()
    await OptiComparisonStop(entry).async_press()
    await OptiDemandHistoryImport(entry).async_press()

    coordinator.async_start_comparison.assert_awaited_once()
    coordinator.async_stop_comparison.assert_awaited_once()
    coordinator.async_import_demand_history.assert_awaited_once()
