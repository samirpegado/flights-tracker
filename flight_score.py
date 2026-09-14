"""Pontuação de combinações ida+volta: preço com conforto mínimo."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _airport_code(value: str) -> str:
    return (value or "").split(" ")[0].upper()


def analyze_legs(legs: list[dict[str, Any]], min_connection: int) -> dict[str, Any]:
    """Detecta troca de aeroporto e conexão curta."""
    airport_change = False
    short_connection = False
    min_layover = None

    for prev, nxt in zip(legs, legs[1:]):
        arr = _airport_code(prev.get("arrival_airport", ""))
        dep = _airport_code(nxt.get("departure_airport", ""))
        if arr and dep and arr != dep:
            airport_change = True

        arr_dt = _parse_dt(prev.get("arrival_datetime"))
        dep_dt = _parse_dt(nxt.get("departure_datetime"))
        if arr_dt and dep_dt:
            layover = int((dep_dt - arr_dt).total_seconds() / 60)
            min_layover = layover if min_layover is None else min(min_layover, layover)
            if layover < min_connection:
                short_connection = True

    return {
        "airport_change": airport_change,
        "short_connection": short_connection,
        "min_layover_minutes": min_layover,
    }


def score_combo(
    outbound: dict[str, Any],
    ret: dict[str, Any] | None,
    policy: dict[str, Any],
) -> tuple[float, list[str]]:
    """Quanto menor o score, melhor o custo-benefício."""
    reasons: list[str] = []
    total = float(outbound.get("price") or 0) + float((ret or {}).get("price") or 0)
    score = total
    reasons.append(f"Preço total R$ {total:.2f}")

    prefer_max = int(policy.get("outbound_max_duration_minutes") or 1200)
    hard_max = int(policy.get("outbound_hard_max_duration_minutes") or 1500)
    out_duration = int(outbound.get("duration_minutes") or 0)

    if out_duration > prefer_max:
        extra = out_duration - prefer_max
        penalty = extra * 2
        score += penalty
        reasons.append(f"Ida {out_duration} min acima do preferido ({prefer_max} min)")
    if out_duration > hard_max:
        score += 800
        reasons.append(f"Ida muito longa ({out_duration} min)")

    stops = int(outbound.get("stops") or 0)
    if stops >= 3:
        score += 400
        reasons.append("Ida com 3+ conexões")

    min_connection = int(policy.get("min_connection_minutes") or 90)
    out_flags = analyze_legs(outbound.get("legs") or [], min_connection)
    if policy.get("avoid_airport_change") and out_flags["airport_change"]:
        score += 450
        reasons.append("Possível troca de aeroporto na ida")
    if policy.get("avoid_self_transfer") and out_flags["short_connection"]:
        score += 350
        reasons.append("Conexão curta na ida")

    if ret:
        if policy.get("return_must_be_direct") and int(ret.get("stops") or 0) > 0:
            score += 5000
            reasons.append("Volta não é direta")
        preferred = [str(a).upper() for a in (policy.get("preferred_airlines_return") or [])]
        if preferred:
            from fli.models import Airline

            preferred_names = set(preferred)
            for code in preferred:
                try:
                    preferred_names.add(getattr(Airline, code).value.upper())
                except AttributeError:
                    continue
            airlines = {
                str(leg.get("airline", "")).upper()
                for leg in (ret.get("legs") or [])
            }
            if airlines and airlines.issubset(preferred_names):
                score -= 200
                reasons.append("Volta na companhia preferida")
            elif airlines:
                reasons.append("Volta fora da companhia preferida")

    return round(score, 2), reasons
