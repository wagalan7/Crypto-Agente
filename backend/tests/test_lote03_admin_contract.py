"""Rotas reais de governança em ASGI isolado, sem lifespan/conta externa.

Carregamos apenas suas definições do main para não iniciar clientes/loops.
FastAPI executa autenticação e parsing reais; o limite do serviço é falso.
As regras persistentes/concorrência são verificadas no harness PostgreSQL.
"""
import ast
import asyncio
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
from typing import Optional, Dict, Any

from fastapi import FastAPI, Header, HTTPException
import httpx


def isolated_app():
    source = ast.parse((Path(__file__).parents[1] / "main.py").read_text())
    names = {"_r13_admin_principal", "_r13_governance_call", "r13_operational_status",
             "r13_register_bundle", "r13_register_approval", "r13_revoke_approval",
             "r13_promote_preselection", "r13_operational_rollback"}
    tree = ast.Module(body=[n for n in source.body if isinstance(n,
        (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names], type_ignores=[])
    app = FastAPI()
    namespace = {"app": app, "Optional": Optional, "Dict": Dict, "Any": Any,
                 "os": os, "Header": Header, "HTTPException": HTTPException,
                 "_check_admin_token": lambda token: None if token == "TEST_ONLY" else {"ok": False},
                 "log": types.SimpleNamespace(error=lambda *_args: None)}
    exec(compile(tree, "main_r13_routes", "exec"), namespace)
    return app


class GovernanceAdminContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.governance = types.ModuleType("services.operational_governance_service")
        for name in ("get_status", "register_bundle", "register_approval", "revoke_approval", "rollback"):
            setattr(self.governance, name, AsyncMock(return_value={"ok": True, "test_only": True}))
        self.db = types.ModuleType("db")
        self.db.DB_ENABLED = True
        self.db.get_session = lambda: None
        self.modules = patch.dict(sys.modules, {"db": self.db,
            "services.operational_governance_service": self.governance})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        import services
        self.attribute = patch.object(services, "operational_governance_service", self.governance, create=True)
        self.attribute.start()
        self.addCleanup(self.attribute.stop)
        self.env = patch.dict(os.environ, {"ADMIN_API_TOKEN": "TEST_ONLY"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app()), base_url="http://isolated")
        self.addAsyncCleanup(self.client.aclose)

    async def test_unauthenticated_approval_cannot_reach_service(self):
        response = await self.client.post("/api/strategy/p05/experiments/1/approval", json={"confirm": True})
        self.assertEqual(response.status_code, 401)
        self.governance.register_approval.assert_not_awaited()

    async def test_unconfigured_token_denied_even_without_mainnet_check(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": ""}):
            response = await self.client.get("/api/strategy/operational/status", headers={"X-Admin-Token": "TEST_ONLY"})
        self.assertEqual(response.status_code, 401)
        self.governance.get_status.assert_not_awaited()

    async def test_operator_is_server_principal_not_secret(self):
        response = await self.client.post("/api/strategy/p05/experiments/1/approval",
            json={"confirm": True}, headers={"X-Admin-Token": "TEST_ONLY"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.governance.register_approval.await_args.kwargs, {"operator": "ADMIN_API_TOKEN"})
        self.assertNotIn("TEST_ONLY", str(self.governance.register_approval.await_args))

    async def test_rollback_requires_literal_confirmation_and_closed_body(self):
        for confirm in (1, "true", False, None):
            response = await self.client.post("/api/strategy/operational/rollback", json={
                "confirm": confirm, "expected_generation": 1, "reason": "TEST", "block_entries": True},
                headers={"X-Admin-Token": "TEST_ONLY"})
            self.assertFalse(response.json()["ok"])
        response = await self.client.post("/api/strategy/operational/rollback", json={
            "confirm": True, "expected_generation": 1, "reason": "TEST", "block_entries": True, "execute_now": True},
            headers={"X-Admin-Token": "TEST_ONLY"})
        self.assertFalse(response.json()["ok"])
        self.governance.rollback.assert_not_awaited()

    async def test_rollback_delegates_cas_and_not_position_actions(self):
        response = await self.client.post("/api/strategy/operational/rollback", json={
            "confirm": True, "expected_generation": 1, "reason": "TEST", "block_entries": True},
            headers={"X-Admin-Token": "TEST_ONLY"})
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.governance.rollback.await_args.kwargs,
            {"expected_generation": 1, "reason": "TEST", "block_entries": True, "operator": "ADMIN_API_TOKEN"})

    async def test_db_absence_is_not_fake_success(self):
        self.db.DB_ENABLED = False
        response = await self.client.get("/api/strategy/operational/status", headers={"X-Admin-Token": "TEST_ONLY"})
        self.assertEqual(response.json()["reason_code"], "GOVERNANCE_DB_UNAVAILABLE")
        self.governance.get_status.assert_not_awaited()

    async def test_reading_status_does_not_invoke_approval_or_rollback(self):
        response = await self.client.get("/api/strategy/operational/status", headers={"X-Admin-Token": "TEST_ONLY"})
        self.assertTrue(response.json()["ok"])
        self.governance.register_approval.assert_not_awaited()
        self.governance.rollback.assert_not_awaited()

    async def test_service_exception_redacted(self):
        self.governance.get_status.side_effect = RuntimeError("TEST_ONLY secret must not escape")
        response = await self.client.get("/api/strategy/operational/status", headers={"X-Admin-Token": "TEST_ONLY"})
        self.assertEqual(response.json()["reason_code"], "GOVERNANCE_UNAVAILABLE")
        self.assertNotIn("secret", response.text)
