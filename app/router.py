"""Complexity scoring + tier selection. `score_complexity` is pure so it is swappable."""

import re
from dataclasses import dataclass
from typing import Any

from app.config import get_routing_config

MODE_QUALITY = "quality"
MODE_BALANCED = "balanced"
MODE_CHEAP = "cheap"
MODES = {MODE_QUALITY, MODE_BALANCED, MODE_CHEAP}

# Weights sum to 1.0; each component is normalised to 0-1 before weighting.
W_LENGTH = 0.20
W_SIGNALS = 0.35
W_MULTISTEP = 0.23
W_TURNS = 0.12
W_SYSTEM = 0.10

LENGTH_SATURATION_TOKENS = 1200
SYSTEM_SATURATION_TOKENS = 500
TURNS_SATURATION = 10

CODE_PATTERNS = (
    r"```",
    r"\bdef\s+\w+\s*\(",
    r"\bclass\s+\w+\b",
    r"\bimport\s+\w+",
    r"\bSELECT\b.+\bFROM\b",
    r"[{};]\s*$",
)
MATH_PATTERNS = (
    r"\$\$?[^$]+\$\$?",
    r"\\(frac|sum|int|sqrt|begin)\b",
    r"\b(derivative|integral|theorem|proof|equation)\b",
    r"\d+\s*[\^*/+-]\s*\d+\s*=",
)
SCHEMA_PATTERNS = (
    r"\bjson[\s_-]?schema\b",
    r'"type"\s*:\s*"(object|array|string)"',
    r"\brespond (only )?(in|with) (valid )?json\b",
)
MULTISTEP_PATTERNS = (
    r"\bstep[\s-]?by[\s-]?step\b",
    r"\bfirst\b.{0,80}\bthen\b",
    r"\bplan\b.{0,40}\b(and|then)\b.{0,40}\bimplement\b",
    r"\b(analyse|analyze|compare|evaluate|refactor|design|architect|debug|optimise|optimize)\b",
    r"\b(explain why|reason about|trade[\s-]?offs?)\b",
    r"^\s*\d+[.)]\s+",
)


@dataclass
class RoutingDecision:
    tier: str
    models: list[str]
    score: float
    mode: str
    override: bool = False
    escalated: bool = False

    @property
    def primary(self) -> str:
        return self.models[0]


def estimate_tokens(text: str) -> int:
    return max(len(text) // 4, 0)


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def any_match(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE | re.MULTILINE) for p in patterns)


def score_complexity(messages: list[dict[str, Any]], tools: list | None = None) -> float:
    """Heuristic 0-1 complexity score. Pure: same input always yields the same score."""
    if not messages:
        return 0.0

    system_text = " ".join(message_text(m) for m in messages if m.get("role") == "system")
    body_text = " ".join(message_text(m) for m in messages if m.get("role") != "system")

    length = min(estimate_tokens(body_text) / LENGTH_SATURATION_TOKENS, 1.0)
    system = min(estimate_tokens(system_text) / SYSTEM_SATURATION_TOKENS, 1.0)

    conversation_turns = len([m for m in messages if m.get("role") != "system"])
    turns = min(max(conversation_turns - 1, 0) / TURNS_SATURATION, 1.0)

    full_text = f"{system_text}\n{body_text}"
    signal_hits = sum(
        [
            any_match(CODE_PATTERNS, full_text),
            any_match(MATH_PATTERNS, full_text),
            any_match(SCHEMA_PATTERNS, full_text),
            bool(tools),
        ]
    )
    signals = min(signal_hits / 2, 1.0)

    multistep_hits = sum(1 for p in MULTISTEP_PATTERNS if re.search(p, full_text, re.I | re.M))
    multistep = min(multistep_hits / 2, 1.0)

    score = (
        W_LENGTH * length
        + W_SIGNALS * signals
        + W_MULTISTEP * multistep
        + W_TURNS * turns
        + W_SYSTEM * system
    )
    return round(min(max(score, 0.0), 1.0), 4)


def _tiers_by_threshold() -> list[tuple[str, dict]]:
    tiers = get_routing_config().tiers
    return sorted(tiers.items(), key=lambda kv: kv[1]["max_score"])


def tier_for_score(score: float) -> str:
    ordered = _tiers_by_threshold()
    for name, spec in ordered:
        if score <= spec["max_score"]:
            return name
    return ordered[-1][0]


def fallback_chain(tier: str) -> list[str]:
    """Models to try in order: the chosen tier, then every tier above it."""
    ordered = _tiers_by_threshold()
    names = [name for name, _ in ordered]
    start = names.index(tier)
    chain: list[str] = []
    for name in names[start:]:
        for model in get_routing_config().tiers[name]["models"]:
            if model not in chain:
                chain.append(model)
    return chain


def tier_of_model(model: str) -> str:
    for name, spec in get_routing_config().tiers.items():
        if model in spec["models"]:
            return name
    return "unknown"


def normalise_mode(mode: str | None) -> str:
    mode = (mode or MODE_BALANCED).strip().lower()
    return mode if mode in MODES else MODE_BALANCED


def route(
    messages: list[dict[str, Any]],
    *,
    mode: str | None = None,
    model_override: str | None = None,
    tools: list | None = None,
    force_cheapest: bool = False,
    escalated: bool = False,
) -> RoutingDecision:
    score = score_complexity(messages, tools)
    mode = normalise_mode(mode)

    if model_override:
        return RoutingDecision(
            tier=tier_of_model(model_override),
            models=[model_override],
            score=score,
            mode=mode,
            override=True,
        )

    ordered = [name for name, _ in _tiers_by_threshold()]
    if force_cheapest or mode == MODE_CHEAP:
        tier = ordered[0]
    elif mode == MODE_QUALITY:
        tier = ordered[-1]
    else:
        tier = tier_for_score(score)

    # A prompt class that repeatedly failed shadow eval is routed one tier higher.
    # An explicit cheap mode or budget-forced downgrade still wins.
    if escalated and not (force_cheapest or mode == MODE_CHEAP):
        tier = ordered[min(ordered.index(tier) + 1, len(ordered) - 1)]

    return RoutingDecision(
        tier=tier,
        models=fallback_chain(tier),
        score=score,
        mode=mode,
        escalated=escalated,
    )

