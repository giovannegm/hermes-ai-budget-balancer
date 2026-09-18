from __future__ import annotations

import json
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from dashboard.plugin_api import (  # noqa: E402
    _runtime_secrets,
    collect_usage_report,
    evaluate_route,
    runtime_claude_guard,
)
from dashboard import plugin_api  # noqa: E402

UTC = timezone.utc


def report(now: datetime, gpt: float, claude: float) -> dict:
    reset = (now + timedelta(days=7)).isoformat()
    return {
        "checked_at": now.isoformat(),
        "gpt": {"ok": True, "remaining_percent": gpt, "used_percent": 100 - gpt, "resets_at": reset},
        "claude": {
            "ok": True,
            "remaining_percent": claude,
            "used_percent": 100 - claude,
            "resets_at": reset,
            "extra_usage_enabled": False,
        },
    }


class Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class BackendTests(unittest.TestCase):
    def test_collector_script_is_bundled_inside_the_plugin(self) -> None:
        self.assertTrue(plugin_api._COLLECTOR_SCRIPT.is_file())
        self.assertEqual(
            plugin_api._COLLECTOR_SCRIPT,
            plugin_api._PLUGIN_ROOT / "scripts" / "saldogptclaude.py",
        )

    def test_collect_usage_report_uses_argv_without_shell(self) -> None:
        expected = report(datetime(2026, 9, 17, tzinfo=UTC), 80, 90)
        calls: list[tuple] = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return Completed(json.dumps(expected))

        self.assertEqual(collect_usage_report(runner=runner), expected)
        argv = calls[0][0][0]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(Path(argv[1]).name, "saldogptclaude.py")
        self.assertTrue(Path(argv[1]).is_absolute())
        self.assertEqual(argv[2], "--json")
        self.assertFalse(calls[0][1].get("shell", False))

    def test_collect_usage_report_never_propagates_subprocess_stderr(self) -> None:
        def runner(*_args, **_kwargs):
            return Completed("", returncode=1, stderr="SECRET-XYZ /private/credentials")

        with self.assertRaises(RuntimeError) as caught:
            collect_usage_report(runner=runner)
        self.assertEqual(str(caught.exception), "saldogptclaude failed")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_usage_refresh_does_not_hold_the_cache_lock_during_subprocess(self) -> None:
        from unittest.mock import patch

        second_started = threading.Event()
        release = threading.Event()
        calls_lock = threading.Lock()
        calls = 0

        def slow_collect():
            nonlocal calls
            with calls_lock:
                calls += 1
                if calls == 2:
                    second_started.set()
            release.wait(timeout=2)
            return report(datetime(2026, 9, 17, tzinfo=UTC), 80, 90)

        plugin_api._report_cache = None
        with patch.object(plugin_api, "collect_usage_report", side_effect=slow_collect):
            threads = [threading.Thread(target=plugin_api._usage_report, kwargs={"force": True}) for _ in range(2)]
            for thread in threads:
                thread.start()
            both_entered = second_started.wait(timeout=0.5)
            release.set()
            for thread in threads:
                thread.join(timeout=2)
        self.assertTrue(both_entered)

    def test_runtime_guard_returns_only_boolean_and_reason(self) -> None:
        status = runtime_claude_guard(
            report_data=report(datetime(2026, 9, 17, tzinfo=UTC), 80, 90),
            secrets={},
            token_resolver=lambda: "sk-ant-oat-private-value",
            auth_runner=lambda: "Login method: Claude Pro account",
        )
        self.assertEqual(status, {"allowed": True, "reason": "oauth_pro"})
        self.assertNotIn("token", status)
        self.assertNotIn("private", json.dumps(status))

    def test_runtime_guard_fails_closed_when_extra_usage_is_unknown(self) -> None:
        data = report(datetime(2026, 9, 17, tzinfo=UTC), 80, 90)
        data["claude"].pop("extra_usage_enabled")
        status = runtime_claude_guard(
            report_data=data,
            secrets={},
            token_resolver=lambda: "sk-ant-oat-private-value",
            auth_runner=lambda: "Login method: Claude Pro account",
        )
        self.assertEqual(status, {"allowed": False, "reason": "extra_usage_unverified"})

    def test_secret_scope_failure_is_not_silently_ignored(self) -> None:
        from unittest.mock import patch

        fake_scope = types.ModuleType("agent.secret_scope")
        fake_constants = types.ModuleType("hermes_constants")

        def fail_scope(_path):
            raise OSError("secret store unavailable")

        fake_scope.build_profile_secret_scope = fail_scope
        fake_constants.get_hermes_home = lambda: Path("/tmp")
        with patch.dict(
            sys.modules,
            {"agent.secret_scope": fake_scope, "hermes_constants": fake_constants},
        ):
            with self.assertRaises(RuntimeError):
                _runtime_secrets()

    def test_new_conversation_routes_and_logs_only_metadata(self) -> None:
        now = datetime(2026, 9, 17, tzinfo=UTC)
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "decisions.jsonl"
            result = evaluate_route(
                {"text": "Implemente este módulo com testes", "phase": "new"},
                report_data=report(now, 100, 100),
                guard_status={"allowed": True, "reason": "oauth_pro"},
                audit_path=log_path,
                now=now,
            )
            self.assertEqual(result["provider"], "anthropic")
            self.assertFalse(result["switch_model"])
            saved = log_path.read_text(encoding="utf-8")
            self.assertNotIn("Implemente", saved)
            self.assertNotIn("text", saved)

    def test_turn_keeps_current_model_when_projection_is_safe(self) -> None:
        now = datetime(2026, 9, 17, 12, tzinfo=UTC)
        with TemporaryDirectory() as directory:
            result = evaluate_route(
                {"text": "Implemente código", "phase": "turn", "current_model": "gpt-5.6-sol"},
                report_data=report(now, 80, 100),
                guard_status={"allowed": True, "reason": "oauth_pro"},
                audit_path=Path(directory) / "decisions.jsonl",
                now=now,
            )
            self.assertEqual(result["provider"], "openai-codex")
            self.assertFalse(result["switch_model"])
            self.assertEqual(result["reason"], "session_stable")

    def test_turn_reports_unknown_quota_without_switching_models(self) -> None:
        now = datetime(2026, 9, 17, 12, tzinfo=UTC)
        data = report(now, 80, 90)
        data["gpt"] = {"ok": False}
        with TemporaryDirectory() as directory:
            result = evaluate_route(
                {"text": "continue", "phase": "turn", "current_model": "gpt-5.6-sol"},
                report_data=data,
                guard_status={"allowed": True, "reason": "oauth_pro"},
                audit_path=Path(directory) / "decisions.jsonl",
                now=now,
            )
            self.assertEqual(result["provider"], "openai-codex")
            self.assertFalse(result["switch_model"])
            self.assertEqual(result["reason"], "quota_unavailable")

    def test_turn_switches_when_observed_burn_will_cross_floor(self) -> None:
        now = datetime(2026, 9, 17, 12, tzinfo=UTC)
        reset_at = now + timedelta(hours=4)
        data = report(now, 40, 90)
        data["gpt"]["resets_at"] = reset_at.isoformat()
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "decisions.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "timestamp": (now - timedelta(hours=1)).isoformat(),
                        "provider": "openai-codex",
                        "model": "gpt-5.6-sol",
                        "weight": "small",
                        "gpt_remaining": 50,
                        "claude_remaining": 90,
                        "reason": "session_stable",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = evaluate_route(
                {"text": "continue", "phase": "turn", "current_model": "gpt-5.6-sol"},
                report_data=data,
                guard_status={"allowed": True, "reason": "oauth_pro"},
                audit_path=log_path,
                now=now,
            )
            self.assertEqual(result["provider"], "anthropic")
            self.assertTrue(result["switch_model"])
            self.assertEqual(result["reason"], "projected_floor_breach")


if __name__ == "__main__":
    unittest.main()
