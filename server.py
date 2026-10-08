#!/usr/bin/env python3
"""
BFT Progress Council MCP — Anti-Loop Guardrail
================================================

By MEOK AI Labs · https://meok.ai · MIT
<!-- mcp-name: io.github.CSOAI-ORG/bft-progress-council-mcp -->

THE PROBLEM
-----------
AI agents burn tokens on loops. They keep "trying" without making real progress.
Cursor, Claude Code, Devin, Aider — every agentic coding tool bleeds tokens when
the agent gets stuck retrying the same approach, claiming progress but doing nothing.

THE SOLUTION
------------
Byzantine Fault Tolerant council that votes on whether real progress is happening.
After every N actions (configurable), the council inspects the last K actions plus
the stated goal and votes:
  - PROGRESS — at least 3 of 5 say "yes, real movement"
  - STALL    — 3 of 5 say "no real progress in last N actions" → halt loop
  - DRIFT    — 3 of 5 say "agent has drifted from the original goal" → halt loop
  - BLOCKED  — 3 of 5 say "environment blocker (rate limit, missing key, etc.)" → escalate

The 5 council members can be 5 different LLMs (Claude, GPT, Gemini, Llama, Step)
OR 5 different prompt personas of one LLM (cheap mode).

USE CASES
---------
- Coding agents (Cursor / Claude Code / Aider): halt when stuck on a bug
- Research agents: halt when going in circles
- Web-browsing agents: halt when retry-looping on a 403
- Multi-MCP orchestration: prevent runaway across A2A calls
- Background workers: prevent infinite token spend

PRICING
-------
Free MIT self-host · £29/mo Starter (managed signing key) · £79/mo Pro
(24h SLA · custom evaluators) · included in A2A Substrate £499/mo
(https://meok.ai/a2a) and Council Universe £1,499/mo
(https://buy.stripe.com/aFa7sNcgAdQS0ZT1Uc8k91t)

Pays for itself in saved API spend within hours of typical agentic work.
"""

from __future__ import annotations
import hashlib
import json
import os
import time
import urllib.request as _meter_urlreq
import urllib.error as _meter_urlerr
from datetime import datetime, timezone
from typing import Optional

from mcp.server.mcpserver import MCPServer as FastMCP  # mcp 2.x: FastMCP renamed MCPServer


mcp = FastMCP("bft-progress-council")

_MEOK_API_KEY = os.environ.get("MEOK_API_KEY", "")
_HMAC_SECRET = os.environ.get("MEOK_HMAC_SECRET", "")


# In-memory session store — production should swap for Redis / Upstash.
# {session_id: {goal, actions: [{ts, action, outcome}], decisions: [...]}}
_SESSIONS: dict[str, dict] = {}


# ────────────────────────────────────────────────────────────────────────
# Council voters (deterministic heuristic scorers)
# ────────────────────────────────────────────────────────────────────────

def _voter_repetition(actions: list[dict], _goal: str) -> tuple[str, str]:
    """Voter 1: detects literal action repetition."""
    if len(actions) < 3:
        return "PROGRESS", "insufficient history to claim stall"
    recent = [a.get("action", "") for a in actions[-5:]]
    if len(set(recent)) <= 2:
        return "STALL", f"only {len(set(recent))} unique actions across last 5"
    return "PROGRESS", "varied actions in recent history"


def _voter_outcome_diversity(actions: list[dict], _goal: str) -> tuple[str, str]:
    """Voter 2: detects identical error / outcome patterns. Checks BLOCKED first."""
    if len(actions) < 3:
        return "PROGRESS", "insufficient history"
    recent_outcomes = [a.get("outcome", "") for a in actions[-5:]]
    # Check for repeated error strings FIRST (more specific than generic identical-outcome stall)
    error_indicators = ["error", "failed", "exception", "denied", "blocked", "rate", "429", "403", "401"]
    error_hits = [o for o in recent_outcomes if any(e in o.lower() for e in error_indicators)]
    if len(error_hits) >= 4:
        return "BLOCKED", f"{len(error_hits)} of recent 5 outcomes contain error/blocked indicators"
    if len([o for o in recent_outcomes if o]) >= 3 and len(set(recent_outcomes)) <= 1:
        return "STALL", "identical outcome across recent actions"
    return "PROGRESS", "outcomes diverse"


def _voter_goal_alignment(actions: list[dict], goal: str) -> tuple[str, str]:
    """Voter 3: detects drift from original goal via simple keyword alignment."""
    if not goal or len(actions) < 3:
        return "PROGRESS", "no goal provided or insufficient history"
    goal_tokens = {t.lower() for t in goal.split() if len(t) > 3}
    recent_actions = " ".join(a.get("action", "") for a in actions[-5:]).lower()
    recent_tokens = {t for t in recent_actions.split() if len(t) > 3}
    if not goal_tokens:
        return "PROGRESS", "goal too short for alignment check"
    overlap = len(goal_tokens & recent_tokens) / max(len(goal_tokens), 1)
    if overlap < 0.1:
        return "DRIFT", f"only {overlap:.0%} goal-token overlap in last 5 actions"
    return "PROGRESS", f"{overlap:.0%} goal-token overlap"


def _voter_action_velocity(actions: list[dict], _goal: str) -> tuple[str, str]:
    """Voter 4: detects spinning — same action burst without environment change."""
    if len(actions) < 5:
        return "PROGRESS", "insufficient history"
    timestamps = [a.get("ts", 0) for a in actions[-5:]]
    if not all(timestamps):
        return "PROGRESS", "missing timestamps"
    deltas = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    avg_delta = sum(deltas) / max(len(deltas), 1)
    if avg_delta < 2.0:  # 5 actions in <8s suggests no human-loop pause
        return "STALL", f"rapid-fire actions ({avg_delta:.1f}s avg delta) suggests spin"
    return "PROGRESS", f"normal pacing ({avg_delta:.1f}s avg delta)"


def _voter_artefact_growth(actions: list[dict], _goal: str) -> tuple[str, str]:
    """Voter 5: detects whether tangible artefacts are accumulating."""
    artefact_keywords = ["write", "create", "deploy", "commit", "publish", "push", "send", "submit", "save"]
    recent = [a.get("action", "").lower() for a in actions[-10:]]
    artefact_hits = sum(1 for a in recent if any(k in a for k in artefact_keywords))
    if artefact_hits == 0 and len(recent) >= 5:
        return "STALL", "no artefact-producing actions in recent history"
    return "PROGRESS", f"{artefact_hits} artefact-producing actions in last 10"


COUNCIL = [
    ("repetition", _voter_repetition),
    ("outcome_diversity", _voter_outcome_diversity),
    ("goal_alignment", _voter_goal_alignment),
    ("action_velocity", _voter_action_velocity),
    ("artefact_growth", _voter_artefact_growth),
]


def _hmac_sign(payload: dict) -> str:
    if not _HMAC_SECRET:
        return "unsigned-no-key-configured"
    import hmac
    body = json.dumps(payload, sort_keys=True).encode()
    return hmac.new(_HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()


# ────────────────────────────────────────────────────────────────────────
# MCP tools
# ────────────────────────────────────────────────────────────────────────

def _server_meter_check(api_key: str = "") -> dict:
    """Calls the live /verify endpoint for server-side metering. Returns the JSON dict.
    Fail-open: if /verify is unreachable or KV isn't configured, returns allowed=True
    (so the local rate-limit in _check_rate_limit remains the safety net)."""
    try:
        data = json.dumps({"api_key": api_key, "tool": ""}).encode()
        req = _meter_urlreq.Request(_METER_URL, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with _meter_urlreq.urlopen(req, timeout=2.5) as r:
            d = json.loads(r.read())
            if isinstance(d, dict) and "allowed" in d:
                return d
    except Exception:
        pass
    return {"allowed": True, "tier": "anonymous", "remaining": 200, "upgrade_url": "https://meok.ai/pricing"}


_METER_URL = "https://proofof.ai/verify"


@mcp.tool()
def start_session(goal: str, session_id: Optional[str] = None) -> dict:
    """
    Start a new BFT Progress Council session.

    Args:
        goal: The original objective the agent is trying to accomplish.
        session_id: Optional explicit ID. Auto-generated if omitted.

    Returns:
        {session_id, goal, started_at, hint}
    """
    sid = session_id or f"s_{int(time.time())}_{os.urandom(4).hex()}"
    _SESSIONS[sid] = {
        "goal": goal,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "actions": [],
        "decisions": [],
    }
    return {
        "session_id": sid,
        "goal": goal,
        "started_at": _SESSIONS[sid]["started_at"],
        "hint": "Call record_action() after every agent action. Call council_vote() periodically (every 5-10 actions) to get a STALL / DRIFT / BLOCKED / PROGRESS verdict.",
    }


@mcp.tool()
def record_action(session_id: str, action: str, outcome: str = "") -> dict:
    """
    Record an agent action against the session log.

    Args:
        session_id: Returned from start_session().
        action: Short description of what the agent just did.
        outcome: Optional outcome / result / error string.

    Returns:
        {session_id, action_count, hint}
    """
    if session_id not in _SESSIONS:
        return {"error": "unknown_session", "hint": "Call start_session() first."}
    _SESSIONS[session_id]["actions"].append({
        "ts": time.time(),
        "action": action,
        "outcome": outcome,
    })
    count = len(_SESSIONS[session_id]["actions"])
    hint = "Continue."
    if count > 0 and count % 5 == 0:
        hint = f"You've recorded {count} actions — recommended to call council_vote() now to check for stall/drift."
    return {"session_id": session_id, "action_count": count, "hint": hint}


@mcp.tool()
def council_vote(session_id: str, lookback: int = 10) -> dict:
    """
    Run the 5-voter Byzantine council on recent actions.

    Args:
        session_id: Returned from start_session().
        lookback: How many recent actions to evaluate (default 10).

    Returns:
        {verdict, votes, signed_attestation, action_recommended}

    Verdicts:
        - PROGRESS: agent should continue
        - STALL: ≥3 voters detect no progress — halt or escalate
        - DRIFT: ≥3 voters detect departure from goal — re-anchor or halt
        - BLOCKED: ≥3 voters detect environment blocker — escalate to human
    """
    sess = _SESSIONS.get(session_id)
    if not sess:
        return {"error": "unknown_session"}
    actions = sess["actions"][-lookback:]
    goal = sess["goal"]

    votes = []
    tally: dict[str, int] = {"PROGRESS": 0, "STALL": 0, "DRIFT": 0, "BLOCKED": 0}
    for name, fn in COUNCIL:
        verdict, reason = fn(actions, goal)
        votes.append({"voter": name, "verdict": verdict, "reason": reason})
        tally[verdict] += 1

    # 3-of-5 BFT threshold (f<n/3 means up to 1 byzantine voter tolerated → need ≥4
    # for true BFT but 3-of-5 majority is the practical halt threshold).
    halt_verdicts = ["STALL", "DRIFT", "BLOCKED"]
    halt_votes = sum(tally[v] for v in halt_verdicts)
    if halt_votes >= 3:
        # Determine which halt reason dominates
        worst = max(halt_verdicts, key=lambda v: tally[v])
        verdict = worst
        action_recommended = {
            "STALL": "halt_loop_and_request_new_approach",
            "DRIFT": "re_anchor_to_original_goal",
            "BLOCKED": "escalate_to_human",
        }[worst]
    else:
        verdict = "PROGRESS"
        action_recommended = "continue"

    attestation_payload = {
        "session_id": session_id,
        "goal": goal,
        "verdict": verdict,
        "tally": tally,
        "voters": votes,
        "lookback": lookback,
        "actions_inspected": len(actions),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    signature = _hmac_sign(attestation_payload)
    sess["decisions"].append({**attestation_payload, "signature": signature})

    return {
        "verdict": verdict,
        "action_recommended": action_recommended,
        "tally": tally,
        "votes": votes,
        "actions_inspected": len(actions),
        "signed_attestation": {
            "payload": attestation_payload,
            "signature": signature,
            "verify_at": "https://verify.meok.ai",
        },
        "ts": attestation_payload["ts"],
    }


@mcp.tool()
def get_session_summary(session_id: str) -> dict:
    """
    Get the full session log + decision history.

    Args:
        session_id: Returned from start_session().

    Returns:
        {goal, started_at, actions, decisions, action_count, decision_count}
    """
    sess = _SESSIONS.get(session_id)
    if not sess:
        return {"error": "unknown_session"}
    return {
        "goal": sess["goal"],
        "started_at": sess["started_at"],
        "action_count": len(sess["actions"]),
        "decision_count": len(sess["decisions"]),
        "actions": sess["actions"][-20:],  # last 20 for brevity
        "decisions": sess["decisions"][-10:],  # last 10 decisions
    }


@mcp.tool()
def estimate_tokens_saved(session_id: str, avg_tokens_per_action: int = 2000) -> dict:
    """
    Estimate how many LLM tokens this council saved by detecting halt conditions.

    Args:
        session_id: Session to analyse.
        avg_tokens_per_action: Estimated avg tokens consumed per agent step.

    Returns:
        {halts_triggered, tokens_likely_saved, cost_likely_saved_gbp}
    """
    sess = _SESSIONS.get(session_id)
    if not sess:
        return {"error": "unknown_session"}
    halts = [d for d in sess["decisions"] if d["verdict"] in ("STALL", "DRIFT", "BLOCKED")]
    # Conservative: assume each halt prevented at least 5 more wasted actions
    assumed_wasted_actions_per_halt = 5
    tokens_saved = len(halts) * assumed_wasted_actions_per_halt * avg_tokens_per_action
    # Claude Opus pricing: ~£0.012 per 1K input + £0.06 per 1K output → assume £0.025/1K blended
    cost_saved_gbp = tokens_saved * 0.025 / 1000
    return {
        "halts_triggered": len(halts),
        "tokens_likely_saved": tokens_saved,
        "cost_likely_saved_gbp": round(cost_saved_gbp, 4),
        "assumption": f"{assumed_wasted_actions_per_halt} wasted actions per halt × {avg_tokens_per_action} tokens × £0.025/1K blended",
        "verify_at": "https://verify.meok.ai",
    }


@mcp.tool()
def list_voters() -> dict:
    """List the 5 council voters and what each detects."""
    return {
        "council_size": len(COUNCIL),
        "voters": [
            {"name": "repetition", "detects": "literal action repetition in last 5 actions"},
            {"name": "outcome_diversity", "detects": "identical outcomes / repeated error strings"},
            {"name": "goal_alignment", "detects": "drift from original goal (keyword overlap)"},
            {"name": "action_velocity", "detects": "rapid-fire spinning with no human-loop pause"},
            {"name": "artefact_growth", "detects": "no tangible artefacts being produced"},
        ],
        "halt_threshold": "3-of-5 (BFT majority)",
        "hint": "Pro tier swaps these heuristics for 5 actual LLM voters (Claude/GPT/Gemini/Llama/Step).",
    }


# ---------------------------------------------------------------------------
# MCP 2026-07-28 wire - header-add migration (2026-10-08)
# ---------------------------------------------------------------------------
# stdio carries no HTTP headers, so Mcp-Method / Mcp-Name are not applicable to
# this transport at runtime. When progress-council-mcp is exposed over HTTP, route the ingress
# through the vendored mcp2026_shim (ShimASGI): it validates Mcp-Method /
# Mcp-Name, injects params._meta.protocolVersion = "2026-07-28" into every
# request, strips Mcp-Session-Id and answers legacy initialize / server-discover
# locally (the session header is never emitted - stateless wire).
# Refs: MIGRATION_NOTE.md, MCP_2026_WIRE_MIGRATION_PLAN_2026-10-07.md (3) + (4).
# ---------------------------------------------------------------------------


def http_app():
    """ASGI app for HTTP exposure, wrapped in the 2026-07-28 wire shim.

    stdio (``mcp.run()``) needs no shim; this is the enable path once the
    server is fronted by an HTTP transport. Bodies are buffered, so responses
    are requested in JSON mode rather than SSE.
    """
    from mcp2026_shim import WIRE_2026, ShimASGI, ShimConfig

    return ShimASGI(
        mcp.streamable_http_app(json_response=True),
        ShimConfig(
            protocol_version=WIRE_2026,
            server_info={"name": "progress-council-mcp", "version": "1.1.8"},
        ),
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()


# ── MEOK monetization layer (Stripe upgrade · PAYG · pricing) ──────────
# Free tier is zero-config. Upgrade to Pro (unlimited) or pay-as-you-go per call.
import os as _meok_os
MEOK_STRIPE_UPGRADE = "https://buy.stripe.com/aFa7sNcgAdQS0ZT1Uc8k91t"  # Pro (unlimited)
MEOK_PAYG_KEY = _meok_os.environ.get("MEOK_PAYG_KEY", "")  # set to enable PAYG (x402 / ~GBP0.05 per call)
MEOK_PRICING = "https://meok.ai/pricing"


def meok_upsell(tier: str = "free") -> dict:
    """Monetization options for free-tier callers: Pro upgrade, PAYG, or pricing page."""
    if tier != "free":
        return {}
    return {"upgrade_url": MEOK_STRIPE_UPGRADE,
            "payg_enabled": bool(MEOK_PAYG_KEY),
            "pricing": MEOK_PRICING}
