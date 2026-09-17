"""R11B1: funções reais das rotas, ASGI local e serviços integralmente falsos.

Não importa o main/boot nem inicia loops; verifica headers, guard e ordem
dos efeitos. Nenhuma rede, exchange, escrita ou mensagem real.
"""
import ast
import logging
import os
from pathlib import Path
import socket
import sys
import traceback
import types
from typing import Optional
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import services

BACKEND = Path(__file__).resolve().parents[1]
ROUTES = ("/api/rotation/apply", "/api/symbol-params/relearn")
MISSING = object()


class AdminRoutes(unittest.TestCase):
    def setUp(self):
        self.net_attempts = []

        def deny_network(*args, **kwargs):
            self.net_attempts.append(True)
            raise AssertionError("rede proibida no R11B1")

        for name in ("getaddrinfo", "create_connection"):
            p = patch.object(socket, name, side_effect=deny_network)
            p.start()
            self.addCleanup(p.stop)
        self.rotation = AsyncMock(return_value={"ok": True, "kind": "rotation-test"})
        self.relearn = AsyncMock(return_value={"ok": True, "kind": "relearn-test"})
        self.production = Mock(return_value=True)
        for short, attrs in (
            ("rotation_service", {"apply_rotation_plan": self.rotation}),
            ("symbol_learning_service", {"relearn_all_from_history": self.relearn}),
            ("shadow_trade_service", {"_exchange_is_production": self.production}),
        ):
            name = f"services.{short}"
            module = types.ModuleType(name)
            module.__dict__.update(attrs)
            old = sys.modules.get(name, MISSING)
            sys.modules[name] = module

            def restore(key=name, previous=old):
                if previous is MISSING:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = previous

            self.addCleanup(restore)
            p = patch.object(services, short, module, create=True)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"ADMIN_API_TOKEN": "synthetic-r11b1-token"})
        p.start()
        self.addCleanup(p.stop)

        tree = ast.parse((BACKEND / "main.py").read_text())
        names = {"rotation_apply", "symbol_params_relearn", "_check_admin_token"}
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name in names]
        self.assertEqual(len(nodes), len(names))
        app = FastAPI()
        scope = {"app": app, "Header": Header, "Optional": Optional, "os": os,
                 "HTTPException": HTTPException, "logging": logging, "traceback": traceback}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BACKEND / "main.py"), "exec"), scope)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def tearDown(self):
        self.assertEqual(self.net_attempts, [])

    def assert_no_effect(self):
        self.rotation.assert_not_awaited()
        self.relearn.assert_not_awaited()

    def test_missing_invalid_blank_and_query_token_never_reach_services(self):
        for route in ROUTES:
            for headers, query in (({}, ""), ({"X-Admin-Token": "wrong"}, ""),
                                   ({"X-Admin-Token": "   "}, ""),
                                   ({}, "?x_admin_token=synthetic-r11b1-token")):
                with self.subTest(route=route, headers=bool(headers), query=bool(query)):
                    response = self.client.post(route + query, headers=headers)
                    self.assertFalse(response.json()["ok"])
                    self.assertNotIn("synthetic-r11b1-token", response.text)
                    self.assert_no_effect()

    def test_valid_header_delegates_once_and_preserves_response(self):
        for route, kind, call in zip(ROUTES, ("rotation-test", "relearn-test"),
                                     (self.rotation, self.relearn)):
            response = self.client.post(route, headers={"X-Admin-Token": "synthetic-r11b1-token"})
            self.assertEqual(response.json(), {"ok": True, "kind": kind})
            call.assert_awaited_once_with()

    def test_no_server_token_blocks_production_even_with_client_header(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": ""}):
            for route in ROUTES:
                response = self.client.post(route, headers={"X-Admin-Token": "anything"})
                self.assertFalse(response.json()["ok"])
                self.assert_no_effect()

    def test_unknown_environment_fails_closed(self):
        self.production.side_effect = RuntimeError("synthetic environment failure")
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": ""}):
            for route in ROUTES:
                self.assertFalse(self.client.post(route).json()["ok"])
                self.assert_no_effect()

    def test_existing_demo_policy_is_preserved(self):
        self.production.return_value = False
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": ""}):
            for route in ROUTES:
                self.assertTrue(self.client.post(route).json()["ok"])
        self.rotation.assert_awaited_once_with()
        self.relearn.assert_awaited_once_with()

    def test_configured_token_is_required_even_in_demo(self):
        self.production.return_value = False
        for route in ROUTES:
            self.assertFalse(self.client.post(route).json()["ok"])
            self.assert_no_effect()

    def test_openapi_exposes_header_for_both_routes(self):
        schema = self.client.get("/openapi.json").json()
        for route in ROUTES:
            params = schema["paths"][route]["post"].get("parameters", [])
            self.assertTrue(any(p["in"] == "header" and p["name"].lower() == "x-admin-token"
                                for p in params), route)


if __name__ == "__main__":
    unittest.main()
