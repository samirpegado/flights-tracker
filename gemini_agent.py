"""Cliente Gemini para extrair política da viagem e escrever briefings."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

def _api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "")


def _model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def gemini_configured() -> bool:
    """Retorna True se a chave do Gemini estiver configurada."""
    return bool(_api_key())


def _extract_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini não retornou candidatos")
    parts = candidates[0].get("content", {}).get("parts") or []
    texts = [p.get("text", "") for p in parts if p.get("text")]
    if not texts:
        raise RuntimeError("Gemini retornou resposta vazia")
    return "\n".join(texts).strip()


def _generate(prompt: str, system: str, json_mode: bool = False) -> str:
    if not _api_key():
        raise RuntimeError("GEMINI_API_KEY não configurada")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{_model()}:generateContent"
    )
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048,
        },
    }
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"

    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            url,
            params={"key": _api_key()},
            json=body,
        )
        response.raise_for_status()
        return _extract_text(response.json())


def fallback_policy(travel: dict[str, Any]) -> dict[str, Any]:
    """Política determinística a partir dos campos estruturados."""
    duration_min = travel.get("duration_min_days")
    duration_max = travel.get("duration_max_days")
    return_max_stops = (travel.get("return_max_stops") or "").upper()
    airlines = travel.get("preferred_airlines") or []
    if isinstance(airlines, str):
        airlines = [a.strip().upper() for a in airlines.split(",") if a.strip()]

    return {
        "outbound_max_duration_minutes": travel.get("outbound_max_duration_minutes") or 1200,
        "outbound_hard_max_duration_minutes": 1500,
        "return_must_be_direct": return_max_stops in {"0", "NON_STOP", "DIRETO"},
        "preferred_airlines_return": airlines,
        "avoid_self_transfer": True,
        "avoid_airport_change": True,
        "min_connection_minutes": 90,
        "duration_min_days": duration_min,
        "duration_max_days": duration_max,
        "passengers": travel.get("passengers") or 1,
        "score_style": "value_not_cheapest",
        "notes": "Política gerada sem Gemini, só com os campos do cadastro.",
    }


def extract_policy(context: str, travel: dict[str, Any]) -> dict[str, Any]:
    """Extrai regras estruturadas do contexto da viagem."""
    base = fallback_policy(travel)
    if not context.strip() or not _api_key():
        return base

    system = (
        "Você extrai regras objetivas de busca de passagens aéreas. "
        "Responda APENAS um JSON válido, sem markdown. "
        "Não invente aeroportos nem datas que não estejam no texto ou nos campos."
    )
    prompt = f"""
Campos já preenchidos pelo usuário:
{json.dumps(travel, ensure_ascii=False, default=str)}

Contexto livre da viagem:
{context}

Preencha este JSON (use null quando não souber):
{{
  "outbound_max_duration_minutes": 1200,
  "outbound_hard_max_duration_minutes": 1500,
  "return_must_be_direct": true,
  "preferred_airlines_return": ["TP"],
  "avoid_self_transfer": true,
  "avoid_airport_change": true,
  "min_connection_minutes": 90,
  "duration_min_days": 12,
  "duration_max_days": 15,
  "passengers": 2,
  "score_style": "value_not_cheapest",
  "notes": "resumo curto das restrições humanas"
}}
"""
    try:
        raw = _generate(prompt, system, json_mode=True)
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return base
        merged = {**base, **{k: v for k, v in parsed.items() if v is not None}}
        return merged
    except Exception as exc:
        print(f"Falha ao extrair política com Gemini: {exc}")
        base["notes"] = f"Fallback sem Gemini: {exc}"
        return base


def write_briefing(
    travel: dict[str, Any],
    policy: dict[str, Any],
    combinations: list[dict[str, Any]],
    previous_best_price: float | None,
) -> str:
    """Escreve um briefing humano das melhores combinações do dia."""
    if not combinations:
        return (
            "Nenhuma combinação de ida e volta encontrada hoje com as regras desta viagem. "
            "Vale tentar de novo em alguns dias — calendários intercontinentais oscilam."
        )

    if not _api_key():
        best = combinations[0]
        total = best.get("total_price")
        return (
            f"Melhor combinação de hoje: R$ {total:.2f} no total "
            f"(ida {best.get('outbound_date')} + volta {best.get('return_date')}). "
            "Gemini não está configurado; este resumo é automático."
        )

    system = (
        "Você é um analista de passagens. Escreva em português do Brasil, "
        "curto e direto (máx. 180 palavras). Não invente voos, preços ou horários. "
        "Compare custo-benefício, não só o preço mais baixo. "
        "Se um voo barato for humanoamente pior (muito longo), diga isso."
    )
    payload = {
        "viagem": {
            "ida": f"{travel.get('from')} → {travel.get('to')}",
            "volta": f"{travel.get('return_from') or travel.get('to')} → {travel.get('return_to') or travel.get('from')}",
            "janela": f"{travel.get('window_start')} a {travel.get('window_end')}",
            "duração": f"{travel.get('duration_min_days')}–{travel.get('duration_max_days')} dias",
        },
        "politica": policy,
        "melhor_preco_anterior": previous_best_price,
        "combinacoes": combinations[:5],
    }
    prompt = (
        "Analise estas combinações reais já buscadas e recomende a melhor. "
        "Cite datas, preço total, duração da ida e se a volta é direta.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, default=str)}"
    )
    try:
        return _generate(prompt, system, json_mode=False)
    except Exception as exc:
        print(f"Falha ao gerar briefing com Gemini: {exc}")
        best = combinations[0]
        return (
            f"Melhor combinação de hoje: R$ {best.get('total_price'):.2f} "
            f"(ida {best.get('outbound_date')} / volta {best.get('return_date')}). "
            f"Não consegui gerar o briefing da IA ({exc})."
        )


def parse_airlines_text(value: Any) -> list[str]:
    """Normaliza companhias preferidas para códigos IATA."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip().upper() for v in value if str(v).strip()]
    return [part.strip().upper() for part in re.split(r"[,\s]+", str(value)) if part.strip()]
