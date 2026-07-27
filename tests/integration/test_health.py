from fastapi.testclient import TestClient
from app.main import app

def test_health_and_openapi():
    with TestClient(app) as client:
        response = client.get("/health"); assert response.status_code == 200; assert response.json()["status"] == "ok"; assert client.get("/openapi.json").status_code == 200

