"""Multi-agent AI orchestrator — 5 agents via fcc-server proxy.

Each agent calls a different provider through the local proxy:
  groq, openrouter, deepseek, mistral, gemini.
The proxy runs at http://localhost:8080 and exposes the Anthropic messages API.
"""

import asyncio
import json
import time
import logging
from dataclasses import dataclass, asdict
from pathlib import Path as _Path
from typing import Any

import httpx

from tools.config import get_settings

logger = logging.getLogger(__name__)

PROXY_URL = get_settings().fcc_proxy_url
TIMEOUT = 60.0  # seconds per agent call


# ─── Agent config ─────────────────────────────────────────────────────────────

@dataclass
class AgentConfig:
    name: str
    provider_type: str
    model: str
    system: str


AGENTS: list[AgentConfig] = [
    AgentConfig(
        name="Momentum Trader",
        provider_type="groq",
        model="llama-3.3-70b-versatile",
        system=(
            "You are a momentum trader. You look for breakouts above key highs, strong volume, "
            "and rising ADX. You VETO longs when RSI(14) > 70 (overbought). "
            "You VETO shorts when RSI(14) < 30 (oversold). "
            "Respond ONLY in JSON: {\"verdict\": \"approve|reject|modify\", "
            "\"confidence\": 0.0-1.0, \"reasoning\": \"1-2 sentences\", "
            "\"suggested_sl\": number|null, \"suggested_tp\": number|null}. "
            "No other text."
        ),
    ),
    AgentConfig(
        name="Mean Reversion",
        provider_type="openrouter",
        model="anthropic/claude-3.5-haiku",
        system=(
            "You are a mean reversion trader. You look for RSI extremes, price at Bollinger "
            "Band extremes, and confirmation that price is NOT in a strong trend. "
            "You VETO when price is below the 200-EMA on a long signal. "
            "You VETO when price is above the 200-EMA on a short signal. "
            "Respond ONLY in JSON: {\"verdict\": \"approve|reject|modify\", "
            "\"confidence\": 0.0-1.0, \"reasoning\": \"1-2 sentences\", "
            "\"suggested_sl\": number|null, \"suggested_tp\": number|null}. "
            "No other text."
        ),
    ),
    AgentConfig(
        name="Order Flow",
        provider_type="deepseek",
        model="deepseek-chat",
        system=(
            "You are an order flow trader. You look for volume spikes, close near the high "
            "(long) or low (short) of the bar, and institutional pressure. "
            "You VETO when volume is below average — no institutional interest. "
            "Respond ONLY in JSON: {\"verdict\": \"approve|reject|modify\", "
            "\"confidence\": 0.0-1.0, \"reasoning\": \"1-2 sentences\", "
            "\"suggested_sl\": number|null, \"suggested_tp\": number|null}. "
            "No other text."
        ),
    ),
    AgentConfig(
        name="Macro/Event",
        provider_type="mistral",
        model="mistral-large-latest",
        system=(
            "You are a macro and event trader. You assess whether news, earnings, "
            "central bank calendars, or macro regime shifts affect this setup. "
            "You VETO setups during high-impact news events or central bank windows. "
            "Respond ONLY in JSON: {\"verdict\": \"approve|reject|modify\", "
            "\"confidence\": 0.0-1.0, \"reasoning\": \"1-2 sentences\", "
            "\"suggested_sl\": number|null, \"suggested_tp\": number|null}. "
            "No other text."
        ),
    ),
]

RISK_MANAGER = AgentConfig(
    name="Risk Manager",
    provider_type="gemini",
    model="gemini-2.0-flash",
    system=(
        "You are a senior risk manager. You have the votes of 4 traders (Momentum, "
        "Mean Reversion, Order Flow, Macro/Event). Each gave a verdict (approve/reject/modify), "
        "confidence, and optional SL/TP suggestions. "
        "Aggregate the votes: if 3+ approve, consensus is approve. "
        "If 3+ reject, consensus is reject. Otherwise modify or reject (cautionary default). "
        "Also set final_size_pct: scale down (below 1.0) if there are concerns. "
        "Set concerns as a list of specific risk flags. "
        "Respond ONLY in JSON: {\"verdict\": \"approve|reject|modify\", "
        "\"final_size_pct\": 0.5-2.0, \"concerns\": [\"str\"], \"summary\": \"str\"}. "
        "No other text."
    ),
)

# Runtime override: load from tools/.agents.json if it exists
_AGENTS_FILE = _Path(__file__).resolve().parent / ".agents.json"
if _AGENTS_FILE.exists():
    try:
        with _AGENTS_FILE.open() as _f:
            _cfg = json.load(_f)
        if "agents" in _cfg and isinstance(_cfg["agents"], list):
            AGENTS = [
                AgentConfig(
                    name=a["name"],
                    provider_type=a["provider_type"],
                    model=a["model"],
                    system=a["system"],
                )
                for a in _cfg["agents"]
                if all(k in a for k in ("name", "provider_type", "model", "system"))
            ]
        if "risk_manager" in _cfg and isinstance(_cfg["risk_manager"], dict):
            rm = _cfg["risk_manager"]
            if all(k in rm for k in ("name", "provider_type", "model", "system")):
                RISK_MANAGER = AgentConfig(
                    name=rm["name"],
                    provider_type=rm["provider_type"],
                    model=rm["model"],
                    system=rm["system"],
                )
    except Exception as _exc:
        # Keep the hard-coded defaults if the file is malformed
        import logging as _log
        _log.getLogger(__name__).warning("Failed to load .agents.json: %s; using defaults", _exc)


# ─── Core HTTP call ────────────────────────────────────────────────────────────

async def call_agent(
    config: AgentConfig,
    user_message: str,
    max_tokens: int = 512,
) -> dict:
    """Call one agent via the fcc-server proxy. Returns {text, latency_ms, tokens} or {error}."""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": "dummy",
        "X-Provider-Type": config.provider_type,
        "anthropic-version": "2023-06-01",
    }
    body: dict[str, Any] = {
        "model": config.model,
        "max_tokens": max_tokens,
        "system": config.system,
        "messages": [{"role": "user", "content": user_message}],
    }
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(PROXY_URL, headers=headers, json=body)
            latency = (time.perf_counter() - t0) * 1000
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}", "latency_ms": latency}
            data = resp.json()
            # Anthropic response shape
            text = ""
            usage = {}
            if "content" in data:
                for block in data["content"]:
                    if block.get("type") == "text":
                        text = block["text"]
            if "usage" in data:
                usage = data["usage"]
            return {
                "text": text,
                "latency_ms": round(latency, 1),
                "tokens": usage.get("output_tokens", 0),
            }
    except httpx.TimeoutException:
        return {"error": "timeout", "latency_ms": (time.perf_counter() - t0) * 1000}
    except Exception as exc:
        return {"error": str(exc), "latency_ms": (time.perf_counter() - t0) * 1000}


# ─── JSON parsing ──────────────────────────────────────────────────────────────

def _strip_json(raw: str) -> dict:
    """Extract and parse JSON from agent response. Tolerant of markdown fences."""
    text = raw.strip()
    text = text.removeprefix("```json").removeprefix("```")
    text = text.removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find the first { ... } block
        start = text.find("{")
        end = text.rfind("}") + 1
        if 0 <= start < end:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        return {"error": "parse_failed", "raw": text[:200]}


# ─── Evaluate a signal ─────────────────────────────────────────────────────────

async def evaluate_signal(
    signal: dict,
    bars_summary: dict,
) -> dict:
    """Run 4 trader agents in parallel, then risk manager. Return aggregate decision."""
    # Build the market context string
    context = (
        f"SYMBOL: {signal.get('symbol', '?')}\n"
        f"STRATEGY: {signal.get('strategy', '?')}\n"
        f"DIRECTION: {signal.get('direction', '?').upper()}\n"
        f"ENTRY: {signal.get('entry', 0)}\n"
        f"STOP LOSS: {signal.get('stop_loss', 0)}\n"
        f"TAKE PROFIT: {signal.get('take_profit', 0)}\n"
        f"CONFIDENCE: {signal.get('confidence', 0)}\n"
        f"REASONING: {signal.get('reasoning', 'N/A')}\n"
        f"\nMARKET CONTEXT:\n"
        f"Last close: {bars_summary.get('last_close', 'N/A')}\n"
        f"ATR(14): {bars_summary.get('atr', 'N/A')}\n"
        f"RSI(14): {bars_summary.get('rsi', 'N/A')}\n"
        f"ADX: {bars_summary.get('adx', 'N/A')}\n"
        f"EMA trend: {bars_summary.get('ema_trend', 'N/A')}\n"
        f"Volume ratio: {bars_summary.get('volume_ratio', 'N/A')}\n"
        f"Recent closes: {bars_summary.get('recent_closes', 'N/A')}\n"
        f"BB position (0=lower,100=upper): {bars_summary.get('bb_position_pct', 'N/A')}\n"
        f"Trend strength score (0-100): {bars_summary.get('trend_score', 'N/A')}\n"
        f"3-bar momentum: {bars_summary.get('momentum_3bar', 'N/A')} "
        f"({bars_summary.get('momentum_3bar_pct', 'N/A')}%)\n"
    )

    user_prompt = (
        "Review the following trading setup and issue your verdict.\n\n"
        f"{context}\n"
        "Your verdict:"
    )

    # Run 4 traders in parallel
    tasks = [call_agent(cfg, user_prompt) for cfg in AGENTS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    votes: list[dict] = []
    latencies: dict[str, float] = {}
    for cfg, result in zip(AGENTS, results):
        latencies[cfg.name] = result.get("latency_ms", 0)
        if isinstance(result, Exception):
            votes.append({"agent": cfg.name, "error": str(result)})
            continue
        if "error" in result:
            votes.append({"agent": cfg.name, "error": result["error"]})
            continue
        parsed = _strip_json(result["text"])
        votes.append({
            "agent": cfg.name,
            "verdict": parsed.get("verdict", "reject"),
            "confidence": parsed.get("confidence", 0.5),
            "reasoning": parsed.get("reasoning", ""),
            "suggested_sl": parsed.get("suggested_sl"),
            "suggested_tp": parsed.get("suggested_tp"),
            "latency_ms": result.get("latency_ms", 0),
        })

    # Risk manager sees all votes
    votes_str = json.dumps(votes, indent=2)
    risk_prompt = (
        "You are the Risk Manager. Below are the votes from 4 traders.\n"
        "Issue your final verdict.\n\n"
        f"Setup:\n{context}\n\n"
        f"Trader votes:\n{votes_str}\n\n"
        "Your verdict:"
    )

    rm_result = await call_agent(RISK_MANAGER, risk_prompt, max_tokens=256)
    latencies[RISK_MANAGER.name] = rm_result.get("latency_ms", 0)

    risk_parsed: dict = {}
    if "error" not in rm_result:
        risk_parsed = _strip_json(rm_result["text"])

    approve_count = sum(1 for v in votes if v.get("verdict") == "approve")
    reject_count = sum(1 for v in votes if v.get("verdict") == "reject")
    modify_count = sum(1 for v in votes if v.get("verdict") == "modify")
    consensus = "approve" if approve_count >= 3 else "reject" if reject_count >= 3 else "modify"

    valid_votes = [v for v in votes if "error" not in v]
    consensus_confidence = (
        sum(v.get("confidence", 0) for v in valid_votes) / max(len(valid_votes), 1)
    )
    return {
        "votes": votes,
        "risk_manager": {
            "verdict": risk_parsed.get("verdict", consensus),
            "final_size_pct": risk_parsed.get("final_size_pct", 1.0),
            "concerns": risk_parsed.get("concerns", []),
            "summary": risk_parsed.get("summary", ""),
            "latency_ms": rm_result.get("latency_ms", 0),
        },
        "consensus": consensus,
        "consensus_confidence": consensus_confidence,
        "approve_count": approve_count,
        "reject_count": reject_count,
        "modify_count": modify_count,
        "agent_latencies": latencies,
    }


# ─── Stub (when proxy not running) ─────────────────────────────────────────────

async def evaluate_signal_stub(signal: dict) -> dict:
    """Return a deterministic stub when the proxy is not reachable."""
    conf = signal.get("confidence", 0.6)
    votes = [
        {"agent": "Momentum Trader", "verdict": "approve" if conf > 0.65 else "reject",
         "confidence": min(1.0, conf + 0.05), "reasoning": "Breakout confirmed on volume.", "suggested_sl": None, "suggested_tp": None},
        {"agent": "Mean Reversion", "verdict": "reject", "confidence": 0.4,
         "reasoning": "Price extended from mean.", "suggested_sl": None, "suggested_tp": None},
        {"agent": "Order Flow", "verdict": "approve" if conf > 0.55 else "modify",
         "confidence": min(1.0, conf), "reasoning": "Volume supporting move.", "suggested_sl": None, "suggested_tp": None},
        {"agent": "Macro/Event", "verdict": "approve", "confidence": 0.6,
         "reasoning": "No major events nearby.", "suggested_sl": None, "suggested_tp": None},
    ]
    approve_count = sum(1 for v in votes if v["verdict"] == "approve")
    consensus = "approve" if approve_count >= 3 else "modify"
    return {
        "votes": votes,
        "risk_manager": {"verdict": consensus, "final_size_pct": 1.0, "concerns": ["STUB MODE — proxy not reachable"], "summary": "Stub evaluation.", "latency_ms": 0},
        "consensus": consensus,
        "consensus_confidence": conf,
        "approve_count": approve_count,
        "reject_count": 1,
        "modify_count": 0,
        "agent_latencies": {v["agent"]: 0 for v in votes},
        "stub": True,
    }


# ─── CLI test ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def _test():
        sig = {
            "symbol": "EURUSD",
            "strategy": "momentum_breakout",
            "direction": "long",
            "entry": 1.0850,
            "stop_loss": 1.0820,
            "take_profit": 1.0910,
            "confidence": 0.72,
            "reasoning": "Breakout above 20-bar high on 1.8x volume, ADX rising.",
        }
        bars_summary = {
            "last_close": 1.0850,
            "atr": 0.0025,
            "rsi": 58,
            "adx": 28,
            "ema_trend": "up",
            "volume_ratio": 1.8,
            "recent_closes": [1.0830, 1.0838, 1.0842, 1.0847, 1.0850],
        }
        print("Evaluating signal (stub mode)...")
        result = await evaluate_signal_stub(sig)
        print(json.dumps(result, indent=2))

    asyncio.run(_test())
