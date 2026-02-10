"""Pytest configuration and fixtures."""

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the API."""
    return TestClient(app)


@pytest.fixture
def sample_proxy_data() -> dict:
    """Sample proxy data for testing."""
    return {
        "host": "proxy.example.com",
        "port": 8080,
        "protocol": "http",
    }


@pytest.fixture
def sample_source_data() -> dict:
    """Sample source data for testing."""
    return {
        "name": "test-source",
        "type": "static",
        "enabled": True,
        "config": {
            "proxies": [
                {"host": "proxy1.example.com", "port": 8080},
                {"host": "proxy2.example.com", "port": 8080},
            ]
        },
    }

