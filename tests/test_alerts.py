"""Incident notification tests with a real HA service bus, no hardware."""
import asyncio
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components import persistent_notification
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest

from custom_components.opti_akku.alerts import HealthAlerts


@pytest.fixture
async def alerts(hass):
    await persistent_notification.async_setup(hass, {})
    entry = MockConfigEntry(domain="opti_akku", title="Opti Test", data={},
                            options={"notification_service": "notify.test_phone"})
    entry.add_to_hass(hass)
    obj = HealthAlerts(hass, entry)
    yield obj
    await obj.async_stop()


def health(**kwargs):
    return {"online": True, "write_enabled": True, "states": {}, **kwargs}


async def test_incident_debounce_no_spam_and_recovery(hass, alerts):
    calls = []
    async def push(call):
        calls.append(call.data)
    hass.services.async_register("notify", "test_phone", push)
    start = dt_util.utcnow()
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start):
        alerts.update(health(online=False))
    assert not alerts._active
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=61)):
        alerts.update(health(online=False))
        for _ in range(10):
            alerts.update(health(online=False))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 1
    assert alerts._active == {"connection"}
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=70)):
        alerts.update(health())
    assert alerts._active
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=131)):
        alerts.update(health())
    assert not alerts._active


async def test_source_incident_debounce_and_reload_lifecycle(hass, alerts):
    """A reload starts a new bounded incident only after a fresh debounce."""
    calls = []

    async def push(call):
        calls.append(call.data)

    hass.services.async_register("notify", "test_phone", push)
    start = dt_util.utcnow()
    missing = health(source_errors={"pv_power": "missing_or_stale"})

    # A transient source outage clears before it becomes an incident.
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start):
        alerts.update(missing)
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=59)):
        alerts.update(health(source_errors={}))
    assert not alerts._active

    # A sustained stale source emits once and repeated updates do not spam.
    for seconds in (100, 161, 279):
        with patch(
            "custom_components.opti_akku.alerts.dt_util.utcnow",
            return_value=start + timedelta(seconds=seconds),
        ):
            alerts.update(missing)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert not alerts._active
    assert not calls
    with patch(
        "custom_components.opti_akku.alerts.dt_util.utcnow",
        return_value=start + timedelta(seconds=280),
    ):
        alerts.update(missing)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert alerts._active == {"sources"}
    assert len(calls) == 1

    # A new instance models config-entry reload or HA restart: the ongoing
    # source failure must satisfy the debounce again before it can notify.
    await alerts.async_stop()
    fresh = HealthAlerts(hass, alerts.entry)
    try:
        with patch(
            "custom_components.opti_akku.alerts.dt_util.utcnow",
            return_value=start + timedelta(seconds=1000),
        ):
            fresh.update(missing)
        assert not fresh._active
        assert len(calls) == 1

        with patch(
            "custom_components.opti_akku.alerts.dt_util.utcnow",
            return_value=start + timedelta(seconds=1181),
        ):
            fresh.update(missing)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert fresh._active == {"sources"}
        assert len(calls) == 2
    finally:
        await fresh.async_stop()


async def test_two_short_stale_source_incidents_do_not_push(hass, alerts):
    """Two short stale incidents should stay visible without noisy pushes."""
    calls = []

    async def push(call):
        calls.append(call.data)

    hass.services.async_register("notify", "test_phone", push)
    start = dt_util.utcnow()
    missing = health(source_errors={"house_consumption": "missing_or_stale"})
    for seconds, data in (
        (0, missing), (119, missing), (120, health()),
        (2100, missing), (2219, missing), (2220, health()),
    ):
        with patch(
            "custom_components.opti_akku.alerts.dt_util.utcnow",
            return_value=start + timedelta(seconds=seconds),
        ):
            alerts.update(data)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not alerts._active
    assert not calls


async def test_nonstale_source_error_gets_its_own_debounce(hass, alerts):
    calls = []

    async def push(call):
        calls.append(call.data)

    hass.services.async_register("notify", "test_phone", push)
    start = dt_util.utcnow()
    stale = health(source_errors={"house_consumption": "missing_or_stale"})
    invalid = health(source_errors={
        "house_consumption": "missing_or_stale", "pv_power": "invalid_value",
    })
    for seconds, data in ((0, stale), (100, stale), (101, invalid), (160, invalid)):
        with patch(
            "custom_components.opti_akku.alerts.dt_util.utcnow",
            return_value=start + timedelta(seconds=seconds),
        ):
            alerts.update(data)
    assert not alerts._active
    assert not calls
    with patch(
        "custom_components.opti_akku.alerts.dt_util.utcnow",
        return_value=start + timedelta(seconds=161),
    ):
        alerts.update(invalid)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert alerts._active == {"sources"}
    assert len(calls) == 1


async def test_malformed_source_errors_do_not_mask_write_alert(alerts):
    assert alerts._source_details(None) == ""
    alerts.update(health(source_errors=None, last_error="Schreibvorgang nicht bestätigt: OSError"))
    assert alerts._active == {"write"}


async def test_source_details_are_bounded_and_sanitize_malformed_source_options(hass, alerts):
    hass.config.language = "de"
    hass.config_entries.async_update_entry(alerts.entry, options={
        **alerts.entry.options,
        "sources": "invalid legacy value",
    })
    details = alerts._source_details({
        f"source_{index}": "missing_or_stale\nextra" for index in range(7)
    })
    assert "source_0 (missing_or_stale extra)" in details
    assert "source_4" in details
    assert "source_5" not in details
    assert "(+2 weitere)" in details
    assert "\n" not in details


async def test_source_alert_names_entities_and_updates_without_extra_push(hass, alerts):
    calls = []

    async def push(call):
        calls.append(call.data["message"])

    hass.services.async_register("notify", "test_phone", push)
    hass.config_entries.async_update_entry(alerts.entry, options={
        **alerts.entry.options,
        "sources": {"pv_power": "sensor.hybrid_ac"},
    })
    start = dt_util.utcnow()
    first = health(source_errors={
        "pv_power": "missing_or_stale",
        "plant:sensor.wallbox": "missing_or_stale",
    })
    second = health(source_errors={"plant:sensor.other_wallbox": "invalid_value"})

    with patch("custom_components.opti_akku.alerts.persistent_notification.async_create") as create:
        with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start):
            alerts.update(first)
        with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=181)):
            alerts.update(first)
        initial_message = create.call_args.args[1]
        assert "pv_power: sensor.hybrid_ac (missing_or_stale)" in initial_message
        assert "sensor.wallbox (missing_or_stale)" in initial_message

        with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=182)):
            alerts.update(second)
        assert create.call_count == 1
        with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=242)):
            alerts.update(second)
        updated_message = create.call_args.args[1]
        assert "sensor.other_wallbox (invalid_value)" in updated_message
        assert "sensor.wallbox" not in updated_message
        assert create.call_count == 2

    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 1
    assert "sensor.hybrid_ac" in calls[0]
    assert "sensor.wallbox" in calls[0]


async def test_tibber_startup_grace_keeps_price_sources_out_of_other_alerts(hass, alerts):
    hass.config_entries.async_update_entry(alerts.entry, options={
        **alerts.entry.options,
        "price_provider": "tibber",
    })
    start = dt_util.utcnow()
    data = health(source_errors={
        "plant:sensor.wallbox": "missing_or_stale",
        "price_current": "missing_or_stale",
        "price_series": "missing_or_stale",
    }, price_provider_error="tibber_fetch_failed")
    with patch("custom_components.opti_akku.alerts.persistent_notification.async_create") as create:
        with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start):
            alerts.update(data)
        with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=181)):
            alerts.update(data)
    message = create.call_args.args[1]
    assert "sensor.wallbox (missing_or_stale)" in message
    assert "price_current" not in message
    assert "price_series" not in message
    assert alerts._active == {"sources"}


async def test_write_alert_immediate_but_not_when_disarmed(hass, alerts):
    alerts.update(health(write_enabled=False, last_error="Schreibvorgang nicht bestätigt: OSError"))
    assert not alerts._active
    alerts.update(health(last_error="Schreibvorgang nicht bestätigt: OSError"))
    assert alerts._active == {"write"}
    await hass.async_block_till_done(wait_background_tasks=True)
    assert alerts.delivery_error == "notification_delivery_failed"


async def test_provider_error_visible_even_with_valid_sources(alerts):
    start = dt_util.utcnow()
    data = health(source_errors={}, price_provider_error="tibber_unavailable")
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start):
        alerts.update(data)
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=61)):
        alerts.update(data)
    assert alerts._active == {"prices"}


async def test_slow_notification_never_blocks_updates_and_cancels(hass, alerts):
    started = asyncio.Event()
    async def push(call):
        started.set()
        await asyncio.Event().wait()
    hass.services.async_register("notify", "test_phone", push)
    alerts.update(health(states={"binary_sensor.opti_block_violation": "on"}))
    await asyncio.wait_for(started.wait(), 1)
    alerts.update(health())
    await asyncio.wait_for(alerts.async_stop(), 1)
    assert alerts._task.done()


async def test_shadow_and_arbitrary_action_are_not_notified(hass, alerts):
    hass.config_entries.async_update_entry(alerts.entry, data={"shadow_mode": True})
    alerts.update(health(states={"binary_sensor.opti_block_violation": "on"}))
    assert not alerts._active
    hass.config_entries.async_update_entry(alerts.entry, options={"notification_service": "switch.turn_on"})
    await alerts._deliver("test")
    assert alerts.delivery_error == "invalid_notification_service"


async def test_explicit_test_uses_selected_service(hass, alerts):
    calls = []
    async def push(call):
        calls.append(call.data)
    hass.services.async_register("notify", "test_phone", push)
    await alerts.async_test()
    assert len(calls) == 1
    assert "Test" in calls[0]["message"]
    assert alerts.delivery_error is None


async def test_unload_and_new_healthy_instance_remove_stale_notifications(hass, alerts):
    with patch("custom_components.opti_akku.alerts.persistent_notification.async_dismiss") as dismiss:
        await alerts.async_stop()
        assert dismiss.call_count == 2
        fresh = HealthAlerts(hass, alerts.entry)
        fresh.update(health())
        assert dismiss.call_count == 3
        fresh.update(health())
        assert dismiss.call_count == 3
        await fresh.async_stop()


async def test_flapping_incident_push_is_throttled(hass, alerts):
    calls = []
    async def push(call):
        calls.append(call.data)
    hass.services.async_register("notify", "test_phone", push)
    start = dt_util.utcnow()
    bad = health(states={"binary_sensor.opti_block_violation": "on"})
    for seconds, data in [(0, bad), (1, health()), (62, health()), (63, bad)]:
        with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start+timedelta(seconds=seconds)):
            alerts.update(data)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 1
    assert alerts._active == {"block"}


async def test_switch_to_ha_only_clears_delivery_error_and_test_succeeds(hass, alerts):
    alerts.delivery_error = "notification_delivery_failed"
    hass.config_entries.async_update_entry(alerts.entry, options={"notification_service": ""})
    alerts.update(health())
    assert alerts.delivery_error is None
    alerts.delivery_error = "notification_delivery_failed"
    await alerts.async_test()
    assert alerts.delivery_error is None


async def test_owed_pause_alerts_even_when_writes_disabled(alerts):
    alerts.update(health(write_enabled=False, pause_pending=True))
    assert alerts._active == {"pause"}


async def test_tibber_startup_recovery_sends_no_notification(hass, alerts):
    hass.config_entries.async_update_entry(alerts.entry, options={**alerts.entry.options, 'price_provider': 'tibber'})
    calls = []
    async def push(call):
        calls.append(call.data)
    hass.services.async_register('notify', 'test_phone', push)
    start = dt_util.utcnow()
    errors = {'price_current':'missing_or_stale', 'price_series':'missing_or_stale'}
    for second in (0, 61, 300, 359):
        data = health(source_errors=dict(errors), price_provider_error='tibber_fetch_failed')
        with patch('custom_components.opti_akku.alerts.dt_util.utcnow', return_value=start+timedelta(seconds=second)):
            alerts.update(data)
        assert data['price_status'] == 'loading'
        assert data['source_errors'] == errors
        assert not alerts._active
    with patch('custom_components.opti_akku.alerts.dt_util.utcnow', return_value=start+timedelta(seconds=359)):
        data = health(price_last_success=start+timedelta(seconds=359))
        alerts.update(data)
    assert data['price_status'] == 'ready'
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []


async def test_tibber_startup_timeout_notifies_once_at_six_minutes(hass, alerts):
    hass.config_entries.async_update_entry(alerts.entry, options={**alerts.entry.options, 'price_provider': 'tibber'})
    calls = []
    async def push(call):
        calls.append(call.data)
    hass.services.async_register('notify', 'test_phone', push)
    data = health(source_errors={'price_current':'missing', 'price_series':'missing'}, price_provider_error='failed')
    start = dt_util.utcnow()
    for second in (0, 60, 359, 360, 420, 900):
        with patch('custom_components.opti_akku.alerts.dt_util.utcnow', return_value=start+timedelta(seconds=second)):
            alerts.update(data)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(calls) == (0 if second < 360 else 1)
    assert alerts._active == {'sources', 'prices'}
    assert data['price_status'] == 'error'


async def test_startup_grace_does_not_mask_other_faults(hass, alerts):
    hass.config_entries.async_update_entry(alerts.entry, options={**alerts.entry.options, 'price_provider': 'tibber'})
    start = dt_util.utcnow()
    data = health(source_errors={'price_current':'missing', 'soc':'missing'}, price_provider_error='failed',
                  last_error='Schreibvorgang nicht bestätigt: Error', pause_pending=True,
                  states={'binary_sensor.opti_block_violation':'on'})
    with patch('custom_components.opti_akku.alerts.dt_util.utcnow', return_value=start):
        alerts.update(data)
    assert alerts._active == {'write', 'block', 'pause'}
    with patch('custom_components.opti_akku.alerts.dt_util.utcnow', return_value=start+timedelta(seconds=61)):
        alerts.update(data)
    assert alerts._active == {'write', 'block', 'pause', 'sources'}


async def test_first_success_ends_grace_for_later_failures(hass, alerts):
    hass.config_entries.async_update_entry(alerts.entry, options={**alerts.entry.options, 'price_provider': 'tibber'})
    start = dt_util.utcnow()
    for second, data in [(0, health(price_last_success=start)),
                         (10, health(price_last_success=start, price_provider_error='failed')),
                         (71, health(price_last_success=start, price_provider_error='failed'))]:
        with patch('custom_components.opti_akku.alerts.dt_util.utcnow', return_value=start+timedelta(seconds=second)):
            alerts.update(data)
    assert alerts._active == {'prices'}
    assert data['price_status'] == 'error'


async def test_shadow_shows_loading_without_notifying(hass, alerts):
    hass.config_entries.async_update_entry(alerts.entry, data={'shadow_mode': True},
        options={**alerts.entry.options, 'price_provider':'tibber'})
    data = health(price_provider_error='failed', source_errors={'price_series':'missing'})
    alerts.update(data)
    assert data['price_status'] == 'loading'
    assert not alerts._active


async def test_strategy_disabled_does_not_claim_price_loading(hass, alerts):
    hass.config_entries.async_update_entry(alerts.entry, options={**alerts.entry.options, 'price_provider': 'tibber'})
    data = health(strategy_enabled=False)
    alerts.update(data)
    assert data['price_status'] == 'not_used'
    assert not alerts._active


async def test_recovery_expected_not_ready_is_one_connection_incident(alerts):
    start = dt_util.utcnow()
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start):
        alerts.update(health(online=False, connection_status={"status": "offline"}))
    with patch(
        "custom_components.opti_akku.alerts.dt_util.utcnow",
        return_value=start + timedelta(seconds=65),
    ):
        alerts.update(
            health(
                connection_status={"status": "not_ready"},
                last_error="Schreibvorgang nicht bestätigt: InverterNotReadyError",
            )
        )
    assert alerts._active == {"connection"}
    # A genuine unconfirmed write or watchdog stall remains immediate.
    alerts.update(
        health(
            connection_status={"status": "recovering"},
            last_error="Schreibvorgang nicht bestätigt: OSError",
        )
    )
    assert "write" in alerts._active


async def test_recovery_never_suppresses_restriction_or_write_stall(alerts):
    alerts.update(
        health(
            connection_status={"status": "recovering"},
            states={
                "binary_sensor.opti_block_violation": "on",
                "binary_sensor.opti_write_stalled": "on",
            },
        )
    )
    assert {"block", "write"} <= alerts._active


async def test_control_inactive_alerts_after_fifteen_minutes_and_clears(hass, alerts):
    """Strategy on but writes off must not silently last a whole night."""
    start = dt_util.utcnow()
    inactive = health(write_enabled=False, control_inactive=True)
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start):
        alerts.update(inactive)
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=899)):
        alerts.update(inactive)
    assert "control" not in alerts._active
    with (patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=900)),
          patch("custom_components.opti_akku.alerts.persistent_notification.async_create") as create):
        alerts.update(inactive)
    assert alerts._active == {"control"}
    assert "not controlling" in create.call_args.args[1] or "steuert den Akku nicht" in create.call_args.args[1]
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=1000)):
        alerts.update(health())
    with patch("custom_components.opti_akku.alerts.dt_util.utcnow", return_value=start + timedelta(seconds=1061)):
        alerts.update(health())
    assert not alerts._active
