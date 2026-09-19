"""Read-only report evidence, coverage, restart and reserve semantics."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json

import pytest

from custom_components.opti_akku.reporting import OperatingReport, reserve_plan

NOW = datetime(2026, 9, 13, 20, tzinfo=UTC)
SETTINGS = {'input_number.opti_peak_verbrauch_kw': 0.8}


def sample(**changes):
    data = {'online': True, 'source_errors': {}, 'strategy_enabled': True,
            'write_enabled': True, 'mode': 'Akku nur Laden', 'reason': 'Peak-Leiter L4 (halten)',
            'command_confirmation': 'idle_or_confirmed',
            'command_evidence': {'status': 'completed', 'execution_basis': 'modbus_write_sequence',
                                 'setpoint_readback': 'not_supported', 'physical_effect': 'not_verified'},
            'states': {'sensor.opti_peak_reserve_soc': '45', 'sensor.opti_soc': '50',
                       'sensor.opti_battery_power_w': 0, 'sensor.opti_house_consumption_w': 1000,
                       'sensor.opti_price_level': 'NORMAL', 'binary_sensor.opti_peak_reserve_aktiv': 'on'},
            'attributes': {'sensor.opti_peak_reserve_soc': {'reserve_ve_soc': 30,
                'horizont_ende': (NOW+timedelta(hours=12)).isoformat(), 'benoetigt_kwh': 5.12,
                'extreme_buffer_kwh': 0.64, 'extreme_buffer_soc': 5,
                'extreme_reserve_soc': 35,
                'peak_stunden_exp': 2, 'peak_stunden_ve': 3.75}}}
    data.update(changes)
    return data


def observe(report, seconds, data=None, **kwargs):
    data = data or sample()
    now = NOW + timedelta(seconds=seconds)
    return report.observe(now, data, reserve_plan(data, SETTINGS, now, shadow=False), **kwargs)


@pytest.mark.parametrize(('changes','shadow','status'), [
    ({}, False, 'hold_requested'), ({}, True, 'observation'),
    ({'write_enabled': False}, False, 'observation'),
    ({'strategy_enabled': False}, False, 'disabled'),
    ({'manual_mode': 'Akku Pause'}, False, 'manual'),
    ({'source_errors': {'price': 'stale'}}, False, 'no_valid_plan'),
    ({'command_confirmation': 'pending'}, False, 'unconfirmed'),
])
def test_plan_describes_request_not_hardware(changes, shadow, status):
    data = sample(**changes)
    result = reserve_plan(data, SETTINGS, NOW, shadow=shadow)
    assert result['status'] == status
    assert result['assumed_load_w'] == 800
    assert 'reserve_held' not in result
    assert result['command_evidence']['physical_effect'] == 'not_verified'
    if status == 'no_valid_plan':
        assert result['planned_reserve_soc'] is None
    else:
        assert result['planned_reserve_soc'] == 45


def test_expired_plan_cannot_look_current():
    assert reserve_plan(sample(), SETTINGS, NOW+timedelta(days=1), shadow=False)['status'] == 'no_valid_plan'


def test_extreme_buffer_is_diagnostic_only_for_a_valid_plan():
    result = reserve_plan(sample(), SETTINGS, NOW, shadow=False)
    assert result['extreme_buffer_kwh'] == pytest.approx(0.64)
    assert result['extreme_buffer_soc'] == 5
    assert result['extreme_reserve_soc'] == 35

    invalid = reserve_plan(
        sample(source_errors={'price': 'stale'}), SETTINGS, NOW, shadow=False
    )
    assert invalid['extreme_buffer_kwh'] is None
    assert invalid['extreme_buffer_soc'] is None
    assert invalid['extreme_reserve_soc'] is None

    malformed = sample()
    malformed['attributes']['sensor.opti_peak_reserve_soc'].update(
        extreme_buffer_kwh='nan', extreme_buffer_soc='unavailable',
        extreme_reserve_soc='unknown'
    )
    result = reserve_plan(malformed, SETTINGS, NOW, shadow=False)
    assert result['planned_reserve_soc'] == 45
    assert result['extreme_buffer_kwh'] is None
    assert result['extreme_buffer_soc'] is None
    assert result['extreme_reserve_soc'] is None


@pytest.mark.parametrize(
    ('changes', 'shadow', 'status', 'hold_threshold'),
    [
        ({}, False, 'hold_requested', 55),
        ({'command_confirmation': 'pending'}, False, 'unconfirmed', 55),
        ({'strategy_enabled': False}, False, 'disabled', None),
        ({'manual_mode': 'Akku Pause'}, False, 'manual', None),
        ({'write_enabled': False}, False, 'observation', None),
        ({}, True, 'observation', None),
    ],
)
def test_extreme_hold_uses_its_own_floor_without_bypassing_status_priority(
    changes, shadow, status, hold_threshold
):
    data = sample(
        reason='Extrempreis-Reserve (Restbedarf fuer Spitzen ueber 60 ct/kWh)',
        **changes,
    )
    data['states']['sensor.opti_peak_reserve_soc'] = '95'
    data['attributes']['sensor.opti_peak_reserve_soc']['extreme_reserve_soc'] = 55
    result = reserve_plan(data, SETTINGS, NOW, shadow=shadow)

    assert result['status'] == status
    assert result['release_threshold_soc'] == 57
    assert result['hold_threshold_soc'] == hold_threshold


def test_malformed_extreme_hold_floor_fails_closed():
    data = sample(reason='Extrempreis-Reserve (Restbedarf fuer Spitzen ueber 60 ct/kWh)')
    data['attributes']['sensor.opti_peak_reserve_soc']['extreme_reserve_soc'] = 'unknown'

    result = reserve_plan(data, SETTINGS, NOW, shadow=False)

    assert result['status'] == 'hold_requested'
    assert result['extreme_reserve_soc'] is None
    assert result['hold_threshold_soc'] is None
    assert result['release_threshold_soc'] is None


def test_energy_gaps_and_restart_do_not_invent_coverage():
    report = OperatingReport()
    data = sample()
    data['states']['sensor.opti_battery_power_w'] = -1000
    observe(report, 0, data)
    out = observe(report, 60, data)
    assert out['totals']['house_energy_kwh'] == pytest.approx(1/60)
    assert out['totals']['battery_discharge_kwh'] == pytest.approx(1/60)
    out = observe(report, 660, data)
    assert out['totals']['house_energy_kwh'] == pytest.approx(1/60)
    assert out['totals']['missing_seconds'] == 600
    new = OperatingReport()
    new.restore(json.loads(json.dumps(report.snapshot(), allow_nan=False)))
    out = observe(new, 690, data)
    assert out['restarts'] == 1
    assert out['totals']['missing_seconds'] == 630
    assert out['totals']['house_energy_kwh'] == pytest.approx(1/60)


def test_peak_consumption_is_not_a_hold_violation():
    report = OperatingReport()
    data = sample(reason='Peak-Leiter L1 (VE entladen)', mode='Akku nur Entladen')
    data['states'].update({'sensor.opti_price_level': 'VERY_EXPENSIVE',
                           'sensor.opti_soc': 25, 'sensor.opti_battery_power_w': -1000})
    observe(report, 0, data)
    out = observe(report, 60, data)
    assert out['current_peak']['min_soc'] == 25
    assert out['current_peak']['battery_discharge_kwh'] == pytest.approx(1/60)
    assert out['totals']['hold_discharge_seconds'] == 0
    end = sample(reason='No peak')
    end['states']['binary_sensor.opti_peak_reserve_aktiv'] = 'off'
    out = observe(report, 90, end)
    assert out['current_peak'] is None
    assert out['last_peak']['end_reason'] == 'price_period_ended'


def test_hold_violation_is_measured_and_missing_is_not_success():
    report = OperatingReport()
    data = sample()
    data['states'].update({'sensor.opti_soc': 40, 'sensor.opti_battery_power_w': -400})
    observe(report, 0, data)
    out = observe(report, 60, data)
    assert out['totals']['hold_discharge_seconds'] == 60
    out = observe(report, 90, sample(online=False))
    assert out['totals']['hold_missing_seconds'] == 30
    assert out['totals']['hold_observed_seconds'] == 60


def test_incidents_are_episodes_and_failed_attempts_are_explicit():
    report = OperatingReport()
    observe(report, 0)
    bad = sample(online=False, source_errors={'a':'stale'}, last_error='old error')
    observe(report, 30, bad, write_failed=True)
    out = observe(report, 60, bad)
    assert out['disconnects'] == 1
    assert out['source_error_episodes'] == 1
    assert out['write_failures'] == 1
    assert out['status'] == 'data_missing'
    for i in range(100):
        observe(report, 90+i*30, sample(), write_failed=True)
    assert len(report.snapshot()['incidents']) == 20


def test_corrupt_store_resets_safely():
    report = OperatingReport()
    observe(report, 0)
    saved = report.snapshot()
    for mutation in ({'totals':None}, {'write_failures':-1}, {'last_observation':'nonsense'}, {'version':99}):
        new = OperatingReport()
        new.restore({**deepcopy(saved), **mutation})
        assert new.data['started_at'] is None


def test_missing_measurement_excludes_both_endpoints():
    report = OperatingReport()
    observe(report, 0)
    data = sample()
    data['states']['sensor.opti_house_consumption_w'] = 'unavailable'
    observe(report, 30, data)
    out = observe(report, 60)
    assert out['totals']['observed_seconds'] == 0
    assert out['totals']['missing_seconds'] == 60


def test_clock_rollback_never_counts_time_twice():
    report = OperatingReport()
    observe(report, 60)
    out = observe(report, 30)
    assert out['status'] == 'data_missing'
    out = observe(report, 90)
    assert out['totals']['observed_seconds'] == 0
    assert out['totals']['missing_seconds'] == 60
    assert out['clock_changes'] == 1
    out = observe(report, 120)
    assert out['totals']['observed_seconds'] == 30


def test_no_active_peak_is_explicit_even_with_past_horizon():
    data = sample()
    data['states']['binary_sensor.opti_peak_reserve_aktiv'] = 'off'
    plan = reserve_plan(data, SETTINGS, NOW+timedelta(days=1), shadow=False)
    assert plan['status'] == 'no_peak'
    assert plan['horizon_end'] is None


def test_persistent_hardware_violation_does_not_stop_hold_measurement():
    report = OperatingReport()
    data = sample(last_error='Sperrverletzung beobachtet; Sollwerte erneuert',
                  command_result_this_update='confirmed')
    data['states']['sensor.opti_battery_power_w'] = -500
    observe(report, 0, data)
    for i in range(1, 11):
        out = observe(report, i*30, data)
    assert out['totals']['hold_discharge_seconds'] == 300
    assert out['totals']['hold_below_threshold_seconds'] == 0
    data['states']['sensor.opti_battery_power_w'] = 0
    data['states']['sensor.opti_soc'] = 30
    observe(report, 330, data)
    out = observe(report, 360, data)
    assert out['totals']['hold_below_threshold_seconds'] == 30
    assert out['totals']['hold_discharge_seconds'] == 330


def test_peak_survives_source_error_and_disconnect():
    report = OperatingReport()
    data = sample(reason='Peak-Leiter L1', mode='Akku nur Entladen')
    data['states']['sensor.opti_price_level'] = 'VERY_EXPENSIVE'
    data['last_write'] = NOW
    observe(report, 0, data)
    observe(report, 30, {**data, 'source_errors': {'ev': 'stale'}})
    observe(report, 60, {**data, 'online': False})
    out = observe(report, 90, data)
    assert out['current_peak']['started_at'] == NOW.isoformat()
    assert out['current_peak']['missing_seconds'] == 90
    assert out['last_peak'] is None
    assert out['current_peak']['plan_at_start']['execution_completed_at'] == NOW.isoformat()
    assert out['current_peak']['plan_at_start']['last_confirmed_command_at'] is None
    json.dumps(report.snapshot(), allow_nan=False)
    out = observe(report, 120, {**data, 'strategy_enabled': False})
    assert out['last_peak']['end_reason'] == 'strategy_disabled'


def test_explained_efficiency_matches_shipped_strategy():
    from pathlib import Path
    import re
    from custom_components.opti_akku import reporting
    resources = json.loads(Path(reporting.__file__).with_name('resources').joinpath('strategy.json').read_text())
    # The displayed assumption must change if the strategy's formula changes.
    block = next(block for block in resources['template_blocks'] if 'peak' in block.get('variables', {}))
    eta = float(re.search(r"set eta = ([0-9.]+)", block['variables']['peak']).group(1))
    assert reserve_plan(sample(), SETTINGS, NOW, shadow=False)['assumed_discharge_efficiency'] == eta


def test_extreme_report_rounding_does_not_change_input_or_hold_threshold():
    data = sample(reason='Extrempreis-Reserve (Restbedarf fuer Spitzen ueber 60 ct/kWh)')
    attrs = data['attributes']['sensor.opti_peak_reserve_soc']
    attrs.update(extreme_buffer_kwh=0.123456, extreme_buffer_soc=1.23456,
                 extreme_reserve_soc=35.6789)
    before = deepcopy(data)
    report = reserve_plan(data, SETTINGS, NOW, shadow=False)
    assert report['extreme_buffer_kwh'] == 0.1235
    assert report['extreme_buffer_soc'] == 1.23
    assert report['extreme_reserve_soc'] == 35.68
    assert report['hold_threshold_soc'] == 35.6789
    assert report['release_threshold_soc'] == pytest.approx(37.6789)
    assert data == before
