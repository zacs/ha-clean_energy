"""Pytest fixtures for Clean Energy tests."""

import pytest


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading of custom integrations in every test."""
    return
