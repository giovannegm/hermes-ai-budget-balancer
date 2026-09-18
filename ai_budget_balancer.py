"""Pure routing policy for the AI budget balancer plugin."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

FLOOR_PERCENT = 3.0
WEEK_SECONDS = 7 * 24 * 60 * 60
GPT_MODEL = "gpt-5.6-sol"
CLAUDE_MODEL = "claude-sonnet-5"

_AUDIT_FIELDS = (
    "timestamp",
    "provider",
    "model",
    "weight",
    "gpt_remaining",
    "claude_remaining",
    "reason",
)

_CODE_TERMS = {
    "bug",
    "código",
    "codigo",
    "implemente",
    "implementar",
    "refatore",
    "refatorar",
    "teste",
    "testes",
    "typescript",
    "python",
    "javascript",
    "api",
    "sql",
    "commit",
    "pull request",
}
_REASONING_TERMS = {
    "arquitetura",
    "decisão",
    "decisao",
    "estratégia",
    "estrategia",
    "compare",
    "analise",
    "análise",
    "planeje",
    "pesquise",
    "explique",
}
_LARGE_TERMS = {
    "completo",
    "extenso",
    "vários arquivos",
    "varios arquivos",
    "codebase",
    "migração",
    "migracao",
    "pesquisa profunda",
}


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        text = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(text)
    else:
        raise ValueError("datetime value is required")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def ideal_remaining(
    now: datetime,
    reset_at: datetime,
    *,
    floor: float = FLOOR_PERCENT,
    cycle_seconds: float = WEEK_SECONDS,
) -> float:
    """Linear remaining-quota target, clamped from 100% to ``floor``."""
    now, reset_at = _as_datetime(now), _as_datetime(reset_at)
    seconds_left = max(0.0, min(float(cycle_seconds), (reset_at - now).total_seconds()))
    return float(floor) + (100.0 - float(floor)) * (seconds_left / float(cycle_seconds))


def _looks_like_oauth(token: str) -> bool:
    token = str(token or "").strip()
    return bool(token) and not token.startswith("sk-ant-api") and token.startswith(("sk-ant-", "eyJ", "cc-"))


def claude_guard(
    secrets: Mapping[str, str],
    resolved_token: str,
    auth_status: str,
    extra_usage_enabled: bool,
) -> tuple[bool, str]:
    """Allow Claude only when billing cannot silently fall through to an API key."""
    if str(secrets.get("ANTHROPIC_API_KEY") or "").strip():
        return False, "api_credential_present"
    if str(secrets.get("ANTHROPIC_AUTH_TOKEN") or "").strip():
        return False, "api_credential_present"
    anthropic_token = str(secrets.get("ANTHROPIC_TOKEN") or "").strip()
    if anthropic_token and not _looks_like_oauth(anthropic_token):
        return False, "api_credential_present"
    if not _looks_like_oauth(resolved_token):
        return False, "oauth_not_verified"
    status_text = str(auth_status or "")
    auth_verified = "Login method: Claude Pro account" in status_text
    try:
        status_data = json.loads(status_text)
    except (TypeError, json.JSONDecodeError):
        status_data = {}
    if isinstance(status_data, dict):
        auth_verified = auth_verified or (
            status_data.get("loggedIn") is True
            and status_data.get("authMethod") == "claude.ai"
            and status_data.get("apiProvider") == "firstParty"
        )
    if not auth_verified:
        return False, "pro_plan_not_verified"
    if extra_usage_enabled:
        return False, "extra_usage_enabled"
    return True, "oauth_pro"


def _term_hits(text: str, terms: set[str]) -> int:
    return sum(1 for term in terms if term in text)


def classify_task(text: str) -> dict[str, Any]:
    """Return coarse, content-free metadata used by the deterministic router."""
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    code_hits = _term_hits(normalized, _CODE_TERMS)
    reasoning_hits = _term_hits(normalized, _REASONING_TERMS)
    large_hits = _term_hits(normalized, _LARGE_TERMS)
    word_count = len(normalized.split())
    if word_count >= 180 or large_hits >= 1:
        weight = "large"
    elif word_count >= 45 or code_hits + reasoning_hits >= 3:
        weight = "medium"
    else:
        weight = "small"
    return {
        "weight": weight,
        "gpt_fit": 1.25 + min(reasoning_hits, 3) * 0.35,
        "claude_fit": 0.85 + min(code_hits, 4) * 0.55,
    }


def _provider_state(item: Mapping[str, Any], now: datetime) -> dict[str, float | datetime | bool]:
    ok = bool(item.get("ok"))
    try:
        remaining = max(0.0, min(100.0, float(item.get("remaining_percent"))))
        reset_at = _as_datetime(item.get("resets_at"))
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "remaining": 0.0, "ideal": 100.0, "surplus": -100.0}
    ideal = ideal_remaining(now, reset_at)
    return {
        "ok": ok,
        "remaining": remaining,
        "reset_at": reset_at,
        "ideal": ideal,
        "surplus": remaining - ideal,
    }


def route_decision(
    report: Mapping[str, Any],
    text: str,
    *,
    now: datetime | None = None,
    claude_allowed: bool = True,
    claude_guard_reason: str = "oauth_pro",
) -> dict[str, Any]:
    """Choose a session model from quota trajectory and task fit."""
    now = _as_datetime(now or datetime.now(timezone.utc))
    classification = classify_task(text)
    gpt = _provider_state(report.get("gpt") or {}, now)
    claude = _provider_state(report.get("claude") or {}, now)

    gpt_usable = bool(gpt["ok"])
    claude_usable = bool(claude["ok"]) and claude_allowed
    if not gpt_usable and not claude_usable:
        raise ValueError("no safe subscription provider is available")

    fit_gap = float(classification["claude_fit"]) - float(classification["gpt_fit"])
    claude_would_be_preferred = (
        float(claude["remaining"]) > FLOOR_PERCENT
        and (fit_gap >= 0.75 or float(claude["surplus"]) > float(gpt["surplus"]))
    )

    # Decision order is deliberate: safety/availability, hard floor, strong task
    # fit, then quota trajectory. GPT wins only a true tie (the soft 60/40 bias).
    if not gpt_usable:
        selected, reason = "anthropic", "gpt_unavailable"
    elif not claude_usable:
        selected = "openai-codex"
        reason = "claude_blocked" if claude_would_be_preferred else "balanced_default"
    elif float(gpt["remaining"]) <= FLOOR_PERCENT < float(claude["remaining"]):
        selected, reason = "anthropic", "quota_balance"
    elif float(claude["remaining"]) <= FLOOR_PERCENT < float(gpt["remaining"]):
        selected, reason = "openai-codex", "quota_balance"
    elif fit_gap >= 0.75:
        selected, reason = "anthropic", "task_fit"
    elif fit_gap <= -0.75:
        selected, reason = "openai-codex", "task_fit"
    elif float(claude["surplus"]) > float(gpt["surplus"]):
        selected, reason = "anthropic", "quota_balance"
    elif float(gpt["surplus"]) > float(claude["surplus"]):
        selected, reason = "openai-codex", "quota_balance"
    else:
        selected, reason = "openai-codex", "balanced_default"

    return {
        "provider": selected,
        "model": CLAUDE_MODEL if selected == "anthropic" else GPT_MODEL,
        "reason": reason,
        "weight": classification["weight"],
        "gpt_remaining": gpt["remaining"],
        "claude_remaining": claude["remaining"],
        "gpt_ideal": gpt["ideal"],
        "claude_ideal": claude["ideal"],
        "claude_allowed": bool(claude_allowed),
        "claude_guard_reason": claude_guard_reason,
    }


def projected_floor_breach(
    current_remaining: float,
    reset_at: datetime,
    previous: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    floor: float = FLOOR_PERCENT,
) -> bool:
    """Whether the observed burn rate reaches ``floor`` before reset."""
    now = _as_datetime(now or datetime.now(timezone.utc))
    current = float(current_remaining)
    if current <= floor:
        return True
    if not previous:
        return False
    try:
        previous_at = _as_datetime(previous.get("at"))
        previous_remaining = float(previous.get("remaining"))
    except (TypeError, ValueError, OverflowError):
        return False
    elapsed_hours = (now - previous_at).total_seconds() / 3600.0
    hours_left = max(0.0, (_as_datetime(reset_at) - now).total_seconds() / 3600.0)
    if elapsed_hours <= 0 or hours_left <= 0:
        return False
    burn_per_hour = max(0.0, previous_remaining - current) / elapsed_hours
    return current - burn_per_hour * hours_left < floor


def append_audit_event(path: Path, event: Mapping[str, Any]) -> None:
    """Append only the approved metadata fields; silently discard all content."""
    safe = {field: event[field] for field in _AUDIT_FIELDS if field in event}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
