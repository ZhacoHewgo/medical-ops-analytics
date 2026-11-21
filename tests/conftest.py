import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def api_client():
    with TestClient(create_app()) as client:
        yield client
