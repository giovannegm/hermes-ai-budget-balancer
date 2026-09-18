#!/usr/bin/env python3
"""Report weekly ChatGPT/Codex and Claude subscription balances."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import selectors
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

VERSION = "0.1.0"
DEFAULT_TIMEOUT = 15.0


def _local_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc).astimezone()
    if isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone()
    return None


def _iso_local(value: Any) -> str | None:
    parsed = _local_datetime(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def _round_percent(value: Any) -> int:
    return max(0, min(100, int(round(float(value)))))


def _read_json_line(proc: subprocess.Popen[str], wanted_id: int, deadline: float) -> dict[str, Any]:
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if not selector.select(remaining):
                break
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == wanted_id:
                if "error" in message:
                    raise RuntimeError(f"Codex app-server: {message['error']}")
                return message
    finally:
        selector.close()
    raise TimeoutError(f"Codex app-server não respondeu ao pedido {wanted_id}")


def get_gpt(timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    codex = shutil.which("codex")
    if not codex:
        raise FileNotFoundError("comando codex não encontrado")

    proc = subprocess.Popen(
        [codex, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdin is not None
        deadline = time.monotonic() + timeout
        initialize = {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "saldogptclaude",
                    "title": "Saldo GPT Claude",
                    "version": VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
        }
        proc.stdin.write(json.dumps(initialize) + "\n")
        proc.stdin.flush()
        _read_json_line(proc, 1, deadline)

        request = {
            "id": 2,
            "method": "account/rateLimits/read",
            "params": {
                "excludeResetCreditDetails": True,
                "supportsLunaReserve": False,
            },
        }
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        result = _read_json_line(proc, 2, deadline).get("result", {})
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    snapshots = result.get("rateLimitsByLimitId") or {}
    snapshot = snapshots.get("codex") if isinstance(snapshots, dict) else None
    if not snapshot:
        snapshot = result.get("rateLimits") or {}

    windows = [snapshot.get("primary"), snapshot.get("secondary")]
    windows = [window for window in windows if isinstance(window, dict)]
    if not windows:
        raise RuntimeError("Codex não retornou a janela de limite")
    weekly = max(windows, key=lambda item: item.get("windowDurationMins") or 0)

    used = _round_percent(weekly.get("usedPercent"))
    reset_credits = result.get("rateLimitResetCredits") or {}
    return {
        "ok": True,
        "plan": str(snapshot.get("planType") or "desconhecido").title(),
        "used_percent": used,
        "remaining_percent": 100 - used,
        "resets_at": _iso_local(weekly.get("resetsAt")),
        "window_minutes": weekly.get("windowDurationMins"),
        "reset_credits_available": int(reset_credits.get("availableCount") or 0),
        "ordinary_usage_allowed": result.get("ordinaryUsageAllowed"),
        "source": "codex app-server account/rateLimits/read",
    }


def _claude_credentials_path() -> pathlib.Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = pathlib.Path(config_dir).expanduser() if config_dir else pathlib.Path.home() / ".claude"
    return root / ".credentials.json"


def _read_claude_credentials() -> tuple[dict[str, Any], pathlib.Path]:
    path = _claude_credentials_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise RuntimeError("credencial OAuth do Claude não encontrada")
    return oauth, path


def _fetch_claude_usage(token: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "User-Agent": f"saldogptclaude/{VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_claude(timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    oauth, _ = _read_claude_credentials()
    try:
        usage = _fetch_claude_usage(str(oauth["accessToken"]), timeout)
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        claude = shutil.which("claude")
        if claude:
            subprocess.run(
                [claude, "auth", "status", "--text"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=min(timeout, 10),
                check=False,
            )
        oauth, _ = _read_claude_credentials()
        usage = _fetch_claude_usage(str(oauth["accessToken"]), timeout)

    weekly = usage.get("seven_day")
    if not isinstance(weekly, dict):
        limits = usage.get("limits") or []
        weekly = next(
            (item for item in limits if isinstance(item, dict) and item.get("kind") == "weekly_all"),
            None,
        )
    if not isinstance(weekly, dict):
        raise RuntimeError("Claude não retornou a janela semanal")

    raw_used = weekly.get("utilization", weekly.get("percent"))
    used = _round_percent(raw_used)
    return {
        "ok": True,
        "plan": str(oauth.get("subscriptionType") or "desconhecido").title(),
        "used_percent": used,
        "remaining_percent": 100 - used,
        "resets_at": _iso_local(weekly.get("resets_at")),
        "extra_usage_enabled": bool((usage.get("extra_usage") or {}).get("is_enabled")),
        "source": "Anthropic /api/oauth/usage",
    }


def _safe_call(name: str, function: Any, timeout: float) -> tuple[str, dict[str, Any]]:
    try:
        return name, function(timeout)
    except Exception as exc:
        return name, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def collect(timeout: float) -> dict[str, Any]:
    results: dict[str, Any] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_safe_call, "gpt", get_gpt, timeout),
            executor.submit(_safe_call, "claude", get_claude, timeout),
        ]
        for future in concurrent.futures.as_completed(futures):
            name, value = future.result()
            results[name] = value
    return {
        "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "gpt": results.get("gpt", {"ok": False, "error": "sem resultado"}),
        "claude": results.get("claude", {"ok": False, "error": "sem resultado"}),
    }


def _format_reset(value: str | None, now: dt.datetime) -> str:
    parsed = _local_datetime(value)
    if parsed is None:
        return "não informado"
    # Providers commonly return :59.5 for a wall-clock minute (for example,
    # 15:59:59.593 for the UI's 16:00). Round for human display.
    if parsed.second >= 30 or parsed.microsecond >= 500_000:
        parsed = parsed + dt.timedelta(minutes=1)
    parsed = parsed.replace(second=0, microsecond=0)
    if parsed.date() == now.date():
        prefix = "hoje"
    elif parsed.date() == now.date() + dt.timedelta(days=1):
        prefix = "amanhã"
    else:
        prefix = parsed.strftime("%d/%m/%Y")
    return f"{prefix} às {parsed.strftime('%H:%M')}"


def print_human(report: dict[str, Any]) -> None:
    now = _local_datetime(report["checked_at"]) or dt.datetime.now().astimezone()
    print(f"Saldo semanal — consulta {now.strftime('%d/%m/%Y %H:%M %z')}")
    for key, label in (("gpt", "GPT/Codex"), ("claude", "Claude")):
        item = report[key]
        if item.get("ok"):
            print(
                f"{label} ({item['plan']}): {item['remaining_percent']}% restante "
                f"({item['used_percent']}% usado) — renova {_format_reset(item.get('resets_at'), now)}."
            )
        else:
            print(f"{label}: consulta falhou — {item.get('error', 'erro desconhecido')}")
    gpt = report.get("gpt", {})
    resets = int(gpt.get("reset_credits_available") or 0)
    if gpt.get("ok") and resets:
        print(f"GPT/Codex: {resets} reset extra disponível (não utilizado).")


def main() -> int:
    parser = argparse.ArgumentParser(description="Mostra o saldo semanal de GPT/Codex e Claude.")
    parser.add_argument("--json", action="store_true", help="imprime JSON estruturado")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="timeout por provedor")
    args = parser.parse_args()

    report = collect(max(3.0, args.timeout))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return 0 if report["gpt"].get("ok") or report["claude"].get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
