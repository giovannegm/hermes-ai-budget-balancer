from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from ai_budget_balancer import (  # noqa: E402
    append_audit_event,
    claude_guard,
    classify_task,
    ideal_remaining,
    projected_floor_breach,
    route_decision,
)

UTC = timezone.utc


def provider(remaining: float, reset_at: datetime, *, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "remaining_percent": remaining,
        "used_percent": 100 - remaining,
        "resets_at": reset_at.isoformat(),
    }


def report(now: datetime, gpt_remaining: float, claude_remaining: float) -> dict:
    reset = now + timedelta(days=7)
    return {
        "checked_at": now.isoformat(),
        "gpt": provider(gpt_remaining, reset),
        "claude": provider(claude_remaining, reset),
    }


class BalancerTests(unittest.TestCase):
    def test_ideal_remaining_follows_linear_path_to_three_percent(self) -> None:
        reset = datetime(2026, 9, 24, tzinfo=UTC)
        self.assertAlmostEqual(ideal_remaining(reset - timedelta(days=7), reset), 100.0)
        self.assertAlmostEqual(ideal_remaining(reset - timedelta(days=3, hours=12), reset), 51.5)
        self.assertAlmostEqual(ideal_remaining(reset, reset), 3.0)
        self.assertAlmostEqual(ideal_remaining(reset + timedelta(hours=1), reset), 3.0)

    def test_claude_guard_fails_closed_for_paid_api_credentials(self) -> None:
        allowed, reason = claude_guard(
            secrets={"ANTHROPIC_API_KEY": "paid-key"},
            resolved_token="sk-ant-oat-test",
            auth_status="Login method: Claude Pro account",
            extra_usage_enabled=False,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "api_credential_present")

    def test_claude_guard_blocks_non_oauth_token_sources(self) -> None:
        for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_TOKEN"):
            with self.subTest(name=name):
                allowed, reason = claude_guard(
                    secrets={name: "sk-ant-api-test"},
                    resolved_token="sk-ant-api-test",
                    auth_status="Login method: Claude Pro account",
                    extra_usage_enabled=False,
                )
                self.assertFalse(allowed)
                self.assertEqual(reason, "api_credential_present")

    def test_claude_guard_accepts_first_party_claude_ai_oauth_status(self) -> None:
        status = json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
            }
        )
        self.assertEqual(
            claude_guard({}, "cc-test-oauth", status, False),
            (True, "oauth_pro"),
        )

    def test_claude_guard_requires_pro_oauth_and_disables_extra_usage(self) -> None:
        self.assertEqual(
            claude_guard({}, "sk-ant-oat-test", "Login method: Claude Pro account", False),
            (True, "oauth_pro"),
        )
        self.assertFalse(claude_guard({}, "sk-ant-oat-test", "Login method: API key", False)[0])
        self.assertEqual(
            claude_guard({}, "sk-ant-oat-test", "Login method: Claude Pro account", True),
            (False, "extra_usage_enabled"),
        )

    def test_general_work_prefers_gpt_when_both_are_on_trajectory(self) -> None:
        now = datetime(2026, 9, 17, tzinfo=UTC)
        result = route_decision(report(now, 100, 100), "Explique este conceito com um exemplo", now=now)
        self.assertEqual(result["provider"], "openai-codex")
        self.assertEqual(result["model"], "gpt-5.6-sol")

    def test_code_implementation_prefers_claude_when_balances_are_equal(self) -> None:
        now = datetime(2026, 9, 17, tzinfo=UTC)
        result = route_decision(
            report(now, 100, 100),
            "Implemente e refatore este módulo Python com testes",
            now=now,
        )
        self.assertEqual(result["provider"], "anthropic")
        self.assertTrue(result["model"].startswith("claude-sonnet"))

    def test_strong_task_fit_precedes_quota_when_provider_is_above_floor(self) -> None:
        now = datetime(2026, 9, 17, tzinfo=UTC)
        result = route_decision(
            report(now, 100, 20),
            "Implemente e refatore este módulo Python com testes",
            now=now,
        )
        self.assertEqual(result["provider"], "anthropic")
        self.assertEqual(result["reason"], "task_fit")

    def test_large_quota_imbalance_overrides_small_default_preference(self) -> None:
        now = datetime(2026, 9, 17, tzinfo=UTC)
        result = route_decision(report(now, 10, 100), "Responda uma pergunta simples", now=now)
        self.assertEqual(result["provider"], "anthropic")
        self.assertEqual(result["reason"], "quota_balance")

    def test_route_never_selects_claude_when_guard_refuses_it(self) -> None:
        now = datetime(2026, 9, 17, tzinfo=UTC)
        result = route_decision(
            report(now, 5, 100),
            "Implemente este recurso",
            now=now,
            claude_allowed=False,
            claude_guard_reason="api_credential_present",
        )
        self.assertEqual(result["provider"], "openai-codex")
        self.assertEqual(result["reason"], "claude_blocked")

    def test_projected_floor_breach_uses_observed_burn_rate(self) -> None:
        now = datetime(2026, 9, 17, 12, tzinfo=UTC)
        previous = {"at": (now - timedelta(hours=1)).isoformat(), "remaining": 50.0}
        self.assertTrue(projected_floor_breach(40.0, now + timedelta(hours=4), previous, now=now))
        self.assertFalse(projected_floor_breach(40.0, now + timedelta(hours=2), previous, now=now))
        self.assertTrue(projected_floor_breach(2.9, now + timedelta(days=2), None, now=now))

    def test_classification_returns_only_non_content_metadata(self) -> None:
        classification = classify_task("Corrija o bug no código e rode os testes")
        self.assertIn(classification["weight"], {"small", "medium", "large"})
        self.assertEqual(set(classification), {"weight", "gpt_fit", "claude_fit"})

    def test_audit_log_whitelists_fields_and_never_writes_content(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.jsonl"
            append_audit_event(
                path,
                {
                    "timestamp": "2026-09-17T12:00:00+00:00",
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "weight": "medium",
                    "gpt_remaining": 80,
                    "claude_remaining": 90,
                    "reason": "quota_balance",
                    "prompt": "segredo",
                    "response": "não gravar",
                    "file": "/private/path",
                },
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved,
                {
                    "timestamp": "2026-09-17T12:00:00+00:00",
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "weight": "medium",
                    "gpt_remaining": 80,
                    "claude_remaining": 90,
                    "reason": "quota_balance",
                },
            )


if __name__ == "__main__":
    unittest.main()
