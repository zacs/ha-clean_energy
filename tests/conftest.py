"""Pytest fixtures for Clean Energy tests."""

import pytest


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Enable loading of custom integrations in every test.

    The fixtures are pulled through ``request`` rather than declared as
    arguments so that a test asking for ``recorder_mock`` still gets the
    recorder stood up *before* ``hass``. As an autouse fixture this would
    otherwise always instantiate ``hass`` first, and the recorder fixture
    asserts that it goes first.
    """
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_mock")
    request.getfixturevalue("enable_custom_integrations")
