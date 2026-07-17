"""Offline FastAPI integration tests for API authentication and CORS."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    FastAPI = None

if FastAPI is not None:
    from src.security import (
        APIAuthenticationError,
        RequestBodyLimitMiddleware,
        authenticate_api_headers,
        parse_cors_origins,
    )


TOKEN = "a" * 48


@unittest.skipIf(FastAPI is None, "project dependencies have not been bootstrapped")
class APIIntegrationTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=parse_cors_origins(""),
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "X-API-Key", "Content-Type"],
        )

        @app.middleware("http")
        async def authenticate(request: Request, call_next):
            if request.url.path.startswith("/api/") and request.method != "OPTIONS":
                try:
                    authenticate_api_headers(request.headers)
                except APIAuthenticationError as exc:
                    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else {}
                    return JSONResponse(
                        status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=headers,
                    )
            return await call_next(request)

        app.add_middleware(RequestBodyLimitMiddleware)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.post("/api/echo")
        async def echo(request: Request):
            return await request.json()

        self.client = TestClient(app)

    def test_health_is_public_but_api_fails_closed_without_server_token(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertEqual(self.client.post("/api/echo", json={}).status_code, 503)

    def test_valid_token_works_and_invalid_token_gets_bearer_challenge(self):
        with patch.dict(os.environ, {"AGENT_BUILDER_API_TOKEN": TOKEN}, clear=True):
            invalid = self.client.post(
                "/api/echo",
                json={"safe": True},
                headers={"Authorization": f"Bearer {'x' * 48}"},
            )
            self.assertEqual(invalid.status_code, 401)
            self.assertEqual(invalid.headers.get("www-authenticate"), "Bearer")

            valid = self.client.post(
                "/api/echo",
                json={"safe": True},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            self.assertEqual(valid.status_code, 200)
            self.assertEqual(valid.json(), {"safe": True})

    def test_cors_preflight_accepts_local_origin_and_rejects_unknown_origin(self):
        headers = {
            "Origin": "http://127.0.0.1:20815",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        }
        allowed = self.client.options("/api/echo", headers=headers)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.headers.get("access-control-allow-origin"),
            "http://127.0.0.1:20815",
        )

        denied = self.client.options(
            "/api/echo",
            headers={**headers, "Origin": "https://attacker.example"},
        )
        self.assertEqual(denied.status_code, 400)


if __name__ == "__main__":
    unittest.main()
