"""Contrato do agregado de pesquisa: leitura, contenção e nenhuma promoção."""
import ast
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.research_batch_service import get_research_status

ROOT = Path(__file__).resolve().parents[1]


class ResearchReportTests(unittest.IsolatedAsyncioTestCase):
    def modules(self, status=None, error=None):
        observation = types.ModuleType("services.decision_observation_service")
        observation.get_status = AsyncMock(
            return_value=status or {"state": "AVAILABLE", "unique_opportunities": 0},
            side_effect=error)
        lab = types.ModuleType("services.offline_replay_service")
        lab.replay_manifest = lambda: {"state": "LOCAL_READY", "promotable": False}
        return observation, lab

    async def test_success_reuses_observer_and_preserves_zero(self):
        observation, lab = self.modules()
        with patch.dict(sys.modules, {observation.__name__: observation, lab.__name__: lab}):
            report = await get_research_status(17)
        observation.get_status.assert_awaited_once_with(days=17)
        self.assertEqual(report["decision_funnel"]["unique_opportunities"], 0)
        self.assertEqual(report["holdout_status"], "SEALED")
        self.assertIs(report["promotable"], False)
        self.assertIs(report["live_changed"], False)
        json.dumps(report, allow_nan=False)

    async def test_read_failure_does_not_hide_lab_or_expose_exception(self):
        observation, lab = self.modules(error=RuntimeError("private database string"))
        with patch.dict(sys.modules, {observation.__name__: observation, lab.__name__: lab}):
            report = await get_research_status()
        self.assertEqual(report["decision_funnel"]["state"], "UNAVAILABLE")
        self.assertEqual(report["offline_lab"]["state"], "LOCAL_READY")
        self.assertNotIn("private database", json.dumps(report))

    async def test_manifest_failure_keeps_funnel(self):
        observation, lab = self.modules()
        lab.replay_manifest = lambda: 1 / 0
        with patch.dict(sys.modules, {observation.__name__: observation, lab.__name__: lab}):
            report = await get_research_status()
        self.assertEqual(report["decision_funnel"]["state"], "AVAILABLE")
        self.assertEqual(report["offline_lab"]["state"], "UNAVAILABLE")

    async def test_window_is_bounded(self):
        observation, lab = self.modules()
        with patch.dict(sys.modules, {observation.__name__: observation, lab.__name__: lab}):
            for value, expected in [(0, 1), (9999, 90), (None, 30), (float("inf"), 30)]:
                report = await get_research_status(value)
                self.assertEqual(report["days"], expected)

    async def test_timeout_is_contained(self):
        observation, lab = self.modules(error=TimeoutError())
        with patch.dict(sys.modules, {observation.__name__: observation, lab.__name__: lab}):
            report = await get_research_status()
        self.assertEqual(report["decision_funnel"]["state"], "UNAVAILABLE")


class IntegrationArchitectureTests(unittest.TestCase):
    def test_flush_is_after_executor_and_in_finally(self):
        tree = ast.parse((ROOT / "main.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                        and n.name == "_server_scan_loop")
        for node in ast.walk(function):
            if not isinstance(node, ast.Try) or not node.finalbody:
                continue
            body = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
            final = ast.unparse(ast.Module(body=node.finalbody, type_ignores=[]))
            if "open_shadow_for_recs(recs_dict)" in body and "flush_pending()" in final:
                self.assertIn("wait_for", final)
                return
        self.fail("flush needs bounded finally after execution")

    def test_every_executor_caller_seals_the_batch(self):
        tree = ast.parse((ROOT / "main.py").read_text())
        callers = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            direct = [s for s in node.body if isinstance(s, ast.Expr)
                      and "await open_shadow_for_recs(" in ast.unparse(s)]
            if not direct:
                continue
            body = ast.unparse(direct[0])
            callers += 1
            final = ast.unparse(ast.Module(body=node.finalbody, type_ignores=[]))
            argument = body.split("await open_shadow_for_recs(")[1].split(")")[0]
            self.assertIn(f"seal_batch({argument})", final, body)
        self.assertEqual(callers, 2)

    def test_no_mutating_research_route(self):
        tree = ast.parse((ROOT / "main.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"post", "put", "delete", "patch"} and node.args:
                    route = ast.literal_eval(node.args[0]) if isinstance(node.args[0], ast.Constant) else ""
                    self.assertFalse(any(x in route for x in ("/research", "/r09", "/r10")))

    def test_ui_has_no_promotion_or_research_request(self):
        text = (ROOT.parent / "frontend/src/components/AssertivenessPanel.tsx").read_text()
        start = text.index("{data?.research_batch &&")
        end = text.index("{/* ── P05.2R", start)
        block = text[start:end]
        for prohibited in ("<button", "fetch(", "onClick", "dangerouslySetInnerHTML",
                           "axios", "href=", "Ativar", "Promover"):
            self.assertNotIn(prohibited, block)
        for required in ("não todos os sinais do mercado", "conta como reavaliação",
                         "funnel?.reevaluations", "Limite de armazenamento atingido",
                         "continuam sendo acompanhadas", "sem trajetória confiável",
                         "não alimentam calibração", "Há lacunas na coleta"):
            self.assertIn(required, block)

    def test_ui_gap_keys_match_backend_telemetry(self):
        from services import decision_observation_service as obs
        text = (ROOT.parent / "frontend/src/components/AssertivenessPanel.tsx").read_text()
        start = text.index("const RESEARCH_GAP_KEYS = [")
        keys = ast.literal_eval(text[start + len("const RESEARCH_GAP_KEYS = "):text.index("]", start) + 1])
        self.assertTrue(set(keys) <= set(obs._TELEMETRY_KEYS), set(keys) - set(obs._TELEMETRY_KEYS))
        loss_keys = {k for k in obs._TELEMETRY_KEYS if "dropped" in k} | {
            "identity_missing", "flush_errors", "flush_timeouts", "flush_cancelled"}
        loss_keys -= {"contention_requeued"}
        self.assertEqual(loss_keys - set(keys), {"db_disabled_dropped"})

    def test_ui_coverage_buckets_match_backend_vocabulary(self):
        from services import decision_observation_service as obs
        text = (ROOT.parent / "frontend/src/components/AssertivenessPanel.tsx").read_text()
        block = text[text.index("const trajectories = {"):text.index("const hasGaps")]
        listed = set(re.findall(r"'([A-Z_]+)'", block))
        self.assertEqual(listed, set(obs.TERMINAL_COVERAGE) | set(obs.OPEN_COVERAGE))


if __name__ == "__main__":
    unittest.main()
