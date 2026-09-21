"""Invalid settings cannot turn the reporting contract into unlimited trust."""
from datetime import UTC, datetime
from types import SimpleNamespace
import math
import pytest
from custom_components.opti_akku.source_quality import measurement_max_age, reported_recently


@pytest.mark.parametrize('value',[True,None,'bad',-1,math.inf,math.nan])
def test_invalid_age_setting_rejects_even_a_current_report(value):
    now=datetime(2026,9,21,tzinfo=UTC)
    for event_based in (True,False):
        assert not reported_recently(SimpleNamespace(last_reported=now),now,
                                     measurement_max_age(value,event_based=event_based))


def test_report_falls_back_to_last_updated_but_not_to_last_changed():
    now=datetime(2026,9,21,tzinfo=UTC)
    assert reported_recently(SimpleNamespace(last_updated=now),now,900)
    assert not reported_recently(SimpleNamespace(last_changed=now),now,900)
    assert not reported_recently(SimpleNamespace(last_reported=now),now.replace(tzinfo=None),900)
