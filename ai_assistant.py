"""
LEVERAGE INVEST - Assistente IA (opcional)
==========================================
Camada de IA sobre o motor de sinais.

IMPORTANTE: a IA NAO calcula sinais. Ela apenas:
  1. Explica em linguagem natural o sinal ja calculado pelo motor tecnico
  2. Responde perguntas livres sobre o mercado/analise

Se a IA falhar (sem key, sem internet, sem cota), o bot continua
funcionando normalmente com o modo por regras. A IA e sempre OPCIONAL.
"""

from __future__ import annotations

import os
import json
import time
from typing import Optional

import requests

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Cache simples para nao repetir a mesma explicacao
_cache: dict = {}
CACHE_TTL = 120  # segundos


def is_enabled() -> bool:
    """IA so fica ativa se houver key configurada."""
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def _call(messages: list, model: str = DEFAULT_MODEL, max_tokens: int = 400,
          temperature: float = 0.4, timeout: int = 30) -> Optional[str]:
    """Chamada crua a API. Retorna texto ou None em qualquer falha."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    try:
        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


SYSTEM_PROMPT = (
    "Voce e o assistente do LEVERAGE INVEST, bot de opcoes binarias na Exnova. "
    "Voce explica sinais tecnicos de forma curta, clara e honesta, em portugues do Brasil. "
    "Regras ao responder:\n"
    "- Seja direto e pratico (maximo ~180 palavras).\n"
    "- Use os dados tecnicos fornecidos; NAO invente numeros.\n"
    "- Nunca prometa lucro nem garanta acerto. Risco existe e deve ser lembrado.\n"
    "- Se o sinal estiver bloqueado ou fraco, diga claramente para NAO operar.\n"
    "- Nao de conselho financeiro; apenas leitura tecnica do cenario.\n"
    "- Use emojis com moderacao."
)


def explain_analysis(active: str, per_tf: dict, direction: Optional[str],
                     strength: int, trend: str, blocked_by: Optional[str],
                     reasons: list, use_cache: bool = True) -> Optional[str]:
    """
    Explica em linguagem natural a analise ja feita pelo motor.
    per_tf: {"M1": {...}, "M5": {...}, "M15": {...}} resumido
    """
    if not is_enabled():
        return None

    cache_key = f"{active}|{direction}|{strength}|{trend}|{blocked_by}"
    if use_cache and cache_key in _cache:
        ts, val = _cache[cache_key]
        if time.time() - ts < CACHE_TTL:
            return val

    dados = {
        "ativo": active,
        "direcao_final": direction or "SEM SINAL",
        "forca": f"{strength}%",
        "tendencia_M15": trend,
        "bloqueio": blocked_by or "nenhum",
        "por_timeframe": per_tf,
        "confluencias": reasons[:8],
    }
    msg = (
        "Analise tecnica gerada pelo motor (use estes dados):\n"
        f"{json.dumps(dados, ensure_ascii=False, indent=2)}\n\n"
        "Explique ao trader: o que o cenario indica, se vale operar agora, "
        "qual expiracao faz sentido e qual o principal risco. Se estiver bloqueado "
        "ou fraco, diga para esperar."
    )
    out = _call(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": msg}],
        max_tokens=450,
    )
    if out and use_cache:
        _cache[cache_key] = (time.time(), out)
    return out


def free_chat(question: str, context: str = "") -> Optional[str]:
    """Responde pergunta livre do usuario, opcionalmente com contexto de analise."""
    if not is_enabled():
        return None
    content = question
    if context:
        content = f"[Contexto tecnico atual: {context}]\n\nPergunta: {question}"
    return _call(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": content}],
        max_tokens=400,
    )


def parse_free_signal_with_ai(text: str) -> Optional[dict]:
    """
    Ultimo recurso: se o parser por regras falhar, pede pra IA estruturar o sinal.
    Retorna dict {timeframe, active, time, direction} ou None.
    """
    if not is_enabled():
        return None
    prompt = (
        "Extraia o sinal de opcoes binarias do texto abaixo. Responda SOMENTE um "
        "JSON valido (sem markdown) com as chaves: timeframe (M1/M5/M15/M30/H1), "
        "active (ativo, ex: EURUSD-OTC), time (HH:MM 24h) e direction (CALL ou PUT). "
        "Se nao houver sinal claro, responda exatamente: null\n\n"
        f"Texto:\n{text[:600]}"
    )
    out = _call([{"role": "user", "content": prompt}], max_tokens=120, temperature=0)
    if not out:
        return None
    out = out.strip().strip("`")
    if "```" in out:
        out = out.split("```")[1].replace("json", "", 1).strip()
    if out.lower() == "null" or not out.startswith("{"):
        return None
    try:
        d = json.loads(out)
        if all(k in d for k in ("timeframe", "active", "time", "direction")):
            return d
    except Exception:
        return None
    return None
