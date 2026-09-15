"""Shared HA fixtures. Tests never connect to real inverters."""

import pytest


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield
