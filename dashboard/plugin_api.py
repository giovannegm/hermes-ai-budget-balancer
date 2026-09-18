"""Scoped REST backend for the AI budget balancer desktop plugin."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from ai_budget_balancer import (  # noqa: E402
    CLAUDE_MODEL,
    GPT_MODEL,
    append_audit_event,
    claude_guard,
    projected_floor_breach,
    route_decision,
)

router = APIRouter()

_REPORT_TTL_SECONDS = 60.0
_report_cache: tuple[float, dict[str, Any]] | None = None
_report_lock = threading.Lock()
_SECRET_NAMES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_TOKEN")


class RouteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    phase: Literal["new", "turn"] = "new"
    current_model: str = Field(default="", max_length=256)
    session_id: str = Field(default="", max_length=256)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _audit_path() -> Path:
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    except Exception:
        home = Path.home() / ".hermes"
    return Path(home) / "ai-budget-balancer" / "decisions.jsonl"


_COLLECTOR_SCRIPT = _PLUGIN_ROOT / "scripts" / "saldogptclaude.py"


def collect_usage_report(runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Read both subscription balances without invoking either model."""
    completed = runner(
        [sys.executable, str(_COLLECTOR_SCRIPT), "--json"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("saldogptclaude failed")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("saldogptclaude returned invalid JSON") from exc
    if not isinstance(payload, dict) or not any((payload.get(name) or {}).get("ok") for name in ("gpt", "claude")):
        raise RuntimeError("no provider balance is available")
    return payload


def _usage_report(*, force: bool = False) -> dict[str, Any]:
    global _report_cache
    checked_at = time.monotonic()
    with _report_lock:
        if not force and _report_cache and checked_at - _report_cache[0] < _REPORT_TTL_SECONDS:
            return _report_cache[1]

    # Never hold the process-wide lock while the external balance probe runs.
    report = collect_usage_report()
    with _report_lock:
        if not force and _report_cache and _report_cache[0] > checked_at:
            return _report_cache[1]
        _report_cache = (time.monotonic(), report)
        return report


def _runtime_secrets() -> dict[str, str]:
    """Resolve only credential presence; values never cross the backend boundary."""
    values: dict[str, str] = {}
    try:
        from agent.secret_scope import build_profile_secret_scope
        from hermes_constants import get_hermes_home

        values.update(build_profile_secret_scope(Path(get_hermes_home())))
    except Exception as exc:
        raise RuntimeError("profile secret scope unavailable") from exc
    for name in _SECRET_NAMES:
        if name not in values and os.environ.get(name) is not None:
            values[name] = os.environ[name]
    return {name: str(values.get(name) or "") for name in _SECRET_NAMES}


def _resolved_anthropic_token() -> str:
    from agent.anthropic_credentials import resolve_anthropic_token

    return str(resolve_anthropic_token() or "")


def _claude_auth_status() -> str:
    completed = subprocess.run(
        ["claude", "auth", "status"],
        shell=False,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return completed.stdout + completed.stderr


def runtime_claude_guard(
    *,
    report_data: Mapping[str, Any],
    secrets: Mapping[str, str] | None = None,
    token_resolver: Callable[[], str] = _resolved_anthropic_token,
    auth_runner: Callable[[], str] = _claude_auth_status,
) -> dict[str, Any]:
    """Return a non-secret Claude safety verdict."""
    try:
        claude_data = report_data.get("claude") or {}
        if "extra_usage_enabled" not in claude_data:
            return {"allowed": False, "reason": "extra_usage_unverified"}
        token = token_resolver()
        auth_status = auth_runner()
        extra_usage = bool(claude_data["extra_usage_enabled"])
        selected_secrets = _runtime_secrets() if secrets is None else secrets
        allowed, reason = claude_guard(selected_secrets, token, auth_status, extra_usage)
    except Exception:
        allowed, reason = False, "oauth_verification_failed"
    return {"allowed": allowed, "reason": reason}


def _provider_for_model(model: str) -> str:
    lowered = str(model or "").strip().lower()
    return "anthropic" if "claude" in lowered or "sonnet" in lowered else "openai-codex"


def _previous_observation(path: Path, provider: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    remaining_field = "claude_remaining" if provider == "anthropic" else "gpt_remaining"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in reversed(lines[-500:]):
        try:
            row = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if row.get("provider") != provider or row.get(remaining_field) is None:
            continue
        return {"at": row.get("timestamp"), "remaining": row.get(remaining_field)}
    return None


def _provider_report(report_data: Mapping[str, Any], provider: str) -> Mapping[str, Any]:
    return report_data.get("claude" if provider == "anthropic" else "gpt") or {}


def _can_use(report_data: Mapping[str, Any], provider: str, guard_status: Mapping[str, Any]) -> bool:
    item = _provider_report(report_data, provider)
    return bool(item.get("ok")) and (provider != "anthropic" or bool(guard_status.get("allowed")))


def evaluate_route(
    body: Mapping[str, Any],
    *,
    report_data: Mapping[str, Any],
    guard_status: Mapping[str, Any],
    audit_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply the session-stability policy and append one content-free decision."""
    now = now or _utc_now()
    decision = route_decision(
        report_data,
        str(body.get("text") or ""),
        now=now,
        claude_allowed=bool(guard_status.get("allowed")),
        claude_guard_reason=str(guard_status.get("reason") or "oauth_verification_failed"),
    )
    phase = str(body.get("phase") or "new")
    switch_model = False

    if phase == "turn" and body.get("current_model"):
        current_provider = _provider_for_model(str(body.get("current_model")))
        current = _provider_report(report_data, current_provider)
        previous = _previous_observation(Path(audit_path), current_provider)
        quota_known = bool(current.get("ok"))
        try:
            breach = projected_floor_breach(
                float(current.get("remaining_percent")),
                current.get("resets_at"),
                previous,
                now=now,
            )
        except (TypeError, ValueError, OverflowError):
            quota_known = False
            breach = False
        if not quota_known:
            decision.update(
                provider=current_provider,
                model=GPT_MODEL if current_provider == "openai-codex" else CLAUDE_MODEL,
                reason="quota_unavailable",
            )
        elif breach:
            alternate = "openai-codex" if current_provider == "anthropic" else "anthropic"
            if _can_use(report_data, alternate, guard_status):
                decision.update(
                    provider=alternate,
                    model=GPT_MODEL if alternate == "openai-codex" else CLAUDE_MODEL,
                    reason="projected_floor_breach",
                )
                switch_model = True
            else:
                decision.update(
                    provider=current_provider,
                    model=GPT_MODEL if current_provider == "openai-codex" else CLAUDE_MODEL,
                    reason="alternate_unavailable",
                )
        else:
            decision.update(
                provider=current_provider,
                model=GPT_MODEL if current_provider == "openai-codex" else CLAUDE_MODEL,
                reason="session_stable",
            )

    decision["switch_model"] = switch_model
    append_audit_event(
        Path(audit_path),
        {
            "timestamp": now.astimezone(timezone.utc).isoformat(),
            "provider": decision["provider"],
            "model": decision["model"],
            "weight": decision["weight"],
            "gpt_remaining": decision["gpt_remaining"],
            "claude_remaining": decision["claude_remaining"],
            "reason": decision["reason"],
        },
    )
    return decision


@router.get("/status")
def status(refresh: bool = Query(False)) -> dict[str, Any]:
    try:
        report_data = _usage_report(force=refresh)
        return {"ok": True, "report": report_data, "claude_guard": runtime_claude_guard(report_data=report_data)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"balance check failed: {exc}") from exc


@router.post("/route")
def route(body: RouteRequest) -> dict[str, Any]:
    try:
        report_data = _usage_report()
        guard_status = runtime_claude_guard(report_data=report_data)
        return {
            "ok": True,
            **evaluate_route(
                body.model_dump(),
                report_data=report_data,
                guard_status=guard_status,
                audit_path=_audit_path(),
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"routing failed: {exc}") from exc
