import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_start_simulation():
    mission_id = "64a7f9e2b3c2a5d6e8f9a1b4"
    response = client.post(f"/simulation/{mission_id}/start")
    assert response.status_code in [200, 404]
    if response.status_code == 200:
        assert "message" in response.json()