"""
LEVERAGE INVEST - Motor de Geracao de Sinais
=============================================
Analise multi-indicador com confluencia ponderada + filtros de gate.

Baseado nas tecnicas consolidadas de mercado (2026):
  - Trend gate (EMA 200 / EMA 9-50): so opera a favor da tendencia maior
  - Confluencia ponderada: RSI, MACD, EMA, Bollinger, Estocastico, Estrutura
  - Filtro ADX: evita mercado lateral (chop)
  - Filtro ATR: evita volatilidade extrema/baixa
  - Filtro de noticias: nao opera perto de eventos economicos

Modulo puro (sem dependencia da Exnova): recebe velas, devolve sinal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional


# ============================================================
# VELAS
# ============================================================

@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    timestamp: int = 0


def to_candles(raw: list) -> List[Candle]:
    """Converte lista de dicts da Exnova em objetos Candle."""
    out: List[Candle] = []
    for c in raw or []:
        try:
            out.append(Candle(
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                timestamp=int(c.get("from", c.get("at", c.get("timestamp", 0))) or 0),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    # Exnova pode devolver em ordem decrescente; normaliza para crescente
    if len(out) >= 2 and out[0].timestamp > out[-1].timestamp and out[0].timestamp:
        out.reverse()
    return out


# ============================================================
# INDICADORES TECNICOS (implementacao pura, sem numpy/talib)
# ============================================================

def ema(values: List[float], period: int) -> List[float]:
    if len(values) < period or period <= 0:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: List[float], period: int) -> List[float]:
    if len(values) < period or period <= 0:
        return []
    out = []
    for i in range(period - 1, len(values)):
        out.append(sum(values[i - period + 1:i + 1]) / period)
    return out


def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) <= period:
        return []
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out = []
    for i in range(period, len(values)):
        if i > period:
            avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
            avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        if avg_l == 0:
            out.append(100.0)
        else:
            rs = avg_g / avg_l
            out.append(100 - (100 / (1 + rs)))
    return out


def macd(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Retorna (linha_macd, linha_sinal, histograma)."""
    ef, es = ema(values, fast), ema(values, slow)
    if not ef or not es:
        return [], [], []
    # alinha os dois EMA pelo fim (mesmo tamanho)
    n = min(len(ef), len(es))
    ef, es = ef[-n:], es[-n:]
    macd_line = [a - b for a, b in zip(ef, es)]
    sig_line = ema(macd_line, signal)
    if not sig_line:
        return macd_line, [], []
    m = min(len(macd_line), len(sig_line))
    macd_line, sig_line = macd_line[-m:], sig_line[-m:]
    hist = [a - b for a, b in zip(macd_line, sig_line)]
    return macd_line, sig_line, hist


def bollinger(values: List[float], period: int = 20, dev: float = 2.0):
    """Retorna (media, banda_superior, banda_inferior, largura_relativa)."""
    if len(values) < period:
        return [], [], [], []
    mid = sma(values, period)
    upper, lower, width = [], [], []
    for i in range(len(mid)):
        window = values[i:i + period]
        m = mid[i]
        var = sum((x - m) ** 2 for x in window) / period
        sd = var ** 0.5
        upper.append(m + dev * sd)
        lower.append(m - dev * sd)
        width.append((2 * dev * sd / m * 100) if m else 0.0)
    return mid, upper, lower, width


def atr(candles: List[Candle], period: int = 14) -> List[float]:
    if len(candles) <= period:
        return []
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        out.append((out[-1] * (period - 1) + tr) / period)
    return out


def adx(candles: List[Candle], period: int = 14) -> List[float]:
    """Average Directional Index - mede forca da tendencia (>20 = tendencia)."""
    if len(candles) <= period + 1:
        return []
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        dn = candles[i - 1].low - candles[i].low
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    def wilder(seq):
        s = [sum(seq[:period])]
        for v in seq[period:]:
            s.append(s[-1] - (s[-1] / period) + v)
        return s

    tr_s, p_s, m_s = wilder(trs), wilder(plus_dm), wilder(minus_dm)
    dx = []
    for i in range(len(tr_s)):
        if tr_s[i] == 0:
            dx.append(0.0)
            continue
        pdi = 100 * p_s[i] / tr_s[i]
        mdi = 100 * m_s[i] / tr_s[i]
        denom = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / denom if denom else 0.0)
    if len(dx) < period:
        return []
    out = [sum(dx[:period]) / period]
    for v in dx[period:]:
        out.append((out[-1] * (period - 1) + v) / period)
    return out


def stochastic(candles: List[Candle], period: int = 14, smooth: int = 3) -> List[float]:
    if len(candles) < period:
        return []
    k_vals = []
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1:i + 1]
        hh = max(c.high for c in window)
        ll = min(c.low for c in window)
        k_vals.append(100 * (candles[i].close - ll) / (hh - ll) if hh != ll else 50.0)
    return sma(k_vals, smooth)


# ============================================================
# FILTRO DE NOTICIAS (calendario economico)
# ============================================================

# Horarios tipicos (UTC) de eventos de alto impacto. Ajustavel.
HIGH_IMPACT_WINDOWS = [
    (12, 30, 13, 30),   # NFP / CPI / dados EUA (08:30 EST)
    (18, 0, 19, 0),     # FOMC / Fed (14:00 EST)
    (11, 45, 13, 0),    # BCE (13:45 CET)
    (6, 0, 8, 0),       # abertura Londres
]

# Ativos afetados por cada janela (vazio = todos)
NEWS_ASSETS = {
    "EURUSD": {"USD", "EUR"}, "GBPUSD": {"USD", "GBP"}, "USDJPY": {"USD", "JPY"},
    "USDCAD": {"USD", "CAD"}, "AUDUSD": {"USD", "AUD"}, "NZDUSD": {"USD", "NZD"},
    "EURJPY": {"EUR", "JPY"}, "EURGBP": {"EUR", "GBP"}, "GBPJPY": {"GBP", "JPY"},
}


def is_news_time(active: str, moment: Optional[datetime] = None, margin_min: int = 15):
    """
    Retorna (bloqueado: bool, motivo: str).
    Evita operar 15 min antes/depois de eventos de alto impacto.
    """
    now = moment or datetime.now()
    mins = now.hour * 60 + now.minute
    for (h1, m1, h2, m2) in HIGH_IMPACT_WINDOWS:
        start = h1 * 60 + m1 - margin_min
        end = h2 * 60 + m2 + margin_min
        if start <= mins <= end:
            return True, f"Janela de noticias ({h1:02d}:{m1:02d}-{h2:02d}:{m2:02d} UTC)"
    return False, ""


# ============================================================
# MOTOR DE SINAIS (confluencia ponderada)
# ============================================================

@dataclass
class SignalResult:
    direction: Optional[str]           # "call" | "put" | None
    strength: int                      # 0-100
    families_agreeing: int
    reasons: List[str] = field(default_factory=list)
    blocked_by: Optional[str] = None   # gate que bloqueou
    trend: str = "lateral"
    price: float = 0.0
    adx_value: float = 0.0

    @property
    def ok(self) -> bool:
        return self.direction is not None and self.blocked_by is None


DEFAULT_WEIGHTS = {
    "trend_ema": 20,       # EMA 9/50 + EMA 200 gate
    "rsi": 15,
    "macd": 15,
    "bollinger": 10,
    "stochastic": 10,
    "structure": 15,       # topos/fundos ascendentes
    "candles": 15,         # padroes de vela
}


def generate_signal(
    candles: List[Candle],
    active: str = "",
    min_strength: int = 60,
    min_families: int = 3,
    adx_min: float = 20.0,
    check_news: bool = True,
    moment: Optional[datetime] = None,
) -> SignalResult:
    """
    Gera um sinal a partir das velas. Só retorna CALL/PUT quando ha
    confluencia suficiente E todos os gates passam.

    Gates (bloqueios duros):
      1. Noticias de alto impacto
      2. ADX < adx_min  -> mercado lateral (chop)
      3. ATR muito baixo -> volatilidade insuficiente
    """
    n = len(candles)
    if n < 60:
        return SignalResult(None, 0, 0, ["Velas insuficientes (precisa >= 60)"],
                            blocked_by="dados", price=candles[-1].close if candles else 0)

    closes = [c.close for c in candles]
    price = closes[-1]

    # ---- Gate 1: noticias ----
    if check_news:
        blocked, motivo = is_news_time(active, moment)
        if blocked:
            return SignalResult(None, 0, 0, [motivo], blocked_by=motivo,
                                price=price)

    # ---- Indicadores ----
    e9 = ema(closes, 9)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200) if n >= 200 else []
    r = rsi(closes, 14)
    macd_line, sig_line, hist = macd(closes)
    bb_mid, bb_up, bb_low, bb_width = bollinger(closes)
    st = stochastic(candles)
    atr_vals = atr(candles, 14)
    adx_vals = adx(candles, 14)

    if not (e9 and e50 and r and hist and atr_vals and adx_vals):
        return SignalResult(None, 0, 0, ["Indicadores nao calculaveis"],
                            blocked_by="dados", price=price)

    adx_now = adx_vals[-1]

    # ---- Gate 2: ADX (tendencia) ----
    if adx_now < adx_min:
        return SignalResult(None, 0, 0,
                            [f"ADX {adx_now:.1f} < {adx_min:.0f} (mercado lateral)"],
                            blocked_by="lateral", price=price, adx_value=adx_now)

    # ---- Gate 3: ATR (volatilidade minima) ----
    atr_pct = atr_vals[-1] / price * 100 if price else 0
    if atr_pct < 0.005:
        return SignalResult(None, 0, 0,
                            [f"Volatilidade muito baixa (ATR {atr_pct:.3f}%)"],
                            blocked_by="sem volatilidade", price=price, adx_value=adx_now)

    # ---- Tendencia maior (EMA 200) ----
    if e200:
        trend_up = price > e200[-1]
        trend = "alta" if trend_up else "baixa"
    else:
        trend_up = e9[-1] > e50[-1]
        trend = "alta" if trend_up else "baixa"

    scores = {"call": 0.0, "put": 0.0}
    reasons: List[str] = []

    # ---- Familia 1: EMA (tendencia) ----
    w = DEFAULT_WEIGHTS["trend_ema"]
    if e9[-1] > e50[-1] and trend_up:
        scores["call"] += w
        reasons.append(f"EMA9>EMA50 e preco acima EMA200 (alta)")
    elif e9[-1] < e50[-1] and not trend_up:
        scores["put"] += w
        reasons.append(f"EMA9<EMA50 e preco abaixo EMA200 (baixa)")
    else:
        reasons.append("EMA sem alinhamento")

    # ---- Familia 2: RSI (momentum com slope) ----
    w = DEFAULT_WEIGHTS["rsi"]
    if len(r) >= 3:
        up_slope = r[-1] > r[-2] > r[-3]
        dn_slope = r[-1] < r[-2] < r[-3]
        if r[-1] < 35 and up_slope:
            scores["call"] += w
            reasons.append(f"RSI {r[-1]:.0f} sobrevendido virando alta")
        elif r[-1] > 65 and dn_slope:
            scores["put"] += w
            reasons.append(f"RSI {r[-1]:.0f} sobrecomprado virando baixa")
        elif 50 < r[-1] < 70 and up_slope:
            scores["call"] += w * 0.6
            reasons.append(f"RSI {r[-1]:.0f} momentum de alta")
        elif 30 < r[-1] < 50 and dn_slope:
            scores["put"] += w * 0.6
            reasons.append(f"RSI {r[-1]:.0f} momentum de baixa")
        else:
            reasons.append(f"RSI {r[-1]:.0f} neutro")

    # ---- Familia 3: MACD ----
    w = DEFAULT_WEIGHTS["macd"]
    if len(hist) >= 2:
        if macd_line[-1] > sig_line[-1] and hist[-1] > hist[-2]:
            scores["call"] += w
            reasons.append("MACD cruzado alta com histograma crescendo")
        elif macd_line[-1] < sig_line[-1] and hist[-1] < hist[-2]:
            scores["put"] += w
            reasons.append("MACD cruzado baixa com histograma caindo")
        else:
            reasons.append("MACD sem confirmacao")

    # ---- Familia 4: Bollinger ----
    w = DEFAULT_WEIGHTS["bollinger"]
    if bb_up and bb_low:
        if price <= bb_low[-1] and trend_up:
            scores["call"] += w
            reasons.append("Preco na banda inferior (reversao a favor da tendencia)")
        elif price >= bb_up[-1] and not trend_up:
            scores["put"] += w
            reasons.append("Preco na banda superior (reversao a favor da tendencia)")
        else:
            reasons.append("Bollinger neutro")

    # ---- Familia 5: Estocastico ----
    w = DEFAULT_WEIGHTS["stochastic"]
    if len(st) >= 2:
        if st[-1] < 25 and st[-1] > st[-2]:
            scores["call"] += w
            reasons.append(f"Estocastico {st[-1]:.0f} saindo de sobrevenda")
        elif st[-1] > 75 and st[-1] < st[-2]:
            scores["put"] += w
            reasons.append(f"Estocastico {st[-1]:.0f} saindo de sobrecompra")
        else:
            reasons.append("Estocastico neutro")

    # ---- Familia 6: Estrutura (topos/fundos) ----
    w = DEFAULT_WEIGHTS["structure"]
    win = candles[-20:]
    highs = [c.high for c in win]
    lows = [c.low for c in win]
    hh = highs[-1] > max(highs[:-1])
    ll = lows[-1] < min(lows[:-1])
    if hh:
        scores["call"] += w
        reasons.append("Rompeu topo recente")
    elif ll:
        scores["put"] += w
        reasons.append("Rompeu fundo recente")
    else:
        reasons.append("Sem rompimento de estrutura")

    # ---- Familia 7: Padroes de vela ----
    w = DEFAULT_WEIGHTS["candles"]
    c0, c1 = candles[-1], candles[-2]
    body0 = abs(c0.close - c0.open)
    rang0 = c0.high - c0.low or 1e-9
    lower_wick = min(c0.open, c0.close) - c0.low
    upper_wick = c0.high - max(c0.open, c0.close)
    bull_engulf = c0.close > c0.open and c0.close >= c1.open and c0.open <= c1.close and c1.close < c1.open
    bear_engulf = c0.close < c0.open and c0.close <= c1.open and c0.open >= c1.close and c1.close > c1.open
    hammer = lower_wick > 2 * body0 and lower_wick > rang0 * 0.5
    star = upper_wick > 2 * body0 and upper_wick > rang0 * 0.5
    if bull_engulf or (hammer and trend_up):
        scores["call"] += w
        reasons.append("Padrao de vela de alta (engolfo/martelo)")
    elif bear_engulf or (star and not trend_up):
        scores["put"] += w
        reasons.append("Padrao de vela de baixa (engolfo/estrela)")
    else:
        reasons.append("Sem padrao de vela claro")

    # ---- Confluencia final ----
    total_possible = sum(DEFAULT_WEIGHTS.values())
    direction = "call" if scores["call"] >= scores["put"] else "put"
    raw = scores[direction]
    strength = int(round(raw / total_possible * 100))

    # Conta familias que votaram na direcao escolhida
    agreeing = 0
    for fam, weight in DEFAULT_WEIGHTS.items():
        marker = {
            "trend_ema": "EMA9",
            "rsi": "RSI ",
            "macd": "MACD",
            "bollinger": "Bollinger",
            "stochastic": "Estocastico",
            "structure": "Rompeu",
            "candles": "Padrao",
        }[fam]
        if any(marker in rs for rs in reasons):
            agreeing += 1

    if strength < min_strength:
        return SignalResult(None, strength, agreeing,
                            reasons + [f"Forca {strength}% < {min_strength}%"],
                            blocked_by=f"confluencia fraca ({strength}%)",
                            trend=trend, price=price, adx_value=adx_now)

    return SignalResult(direction, strength, agreeing, reasons,
                        trend=trend, price=price, adx_value=adx_now)


# ============================================================
# PARSER FLEXIVEL DE SINAIS (aceita varios formatos)
# ============================================================

DIRECTION_MAP = {
    "call": "CALL", "compra": "CALL", "buy": "CALL", "cima": "CALL",
    "alta": "CALL", "subir": "CALL", "verde": "CALL", "c": "CALL", "🟢": "CALL",
    "put": "PUT", "venda": "PUT", "sell": "PUT", "baixo": "PUT",
    "baixa": "PUT", "descer": "PUT", "vermelha": "PUT", "v": "PUT", "🔴": "PUT",
}

TF_MAP = {
    "m1": "M1", "1m": "M1", "1min": "M1", "m": "M1",
    "m5": "M5", "5m": "M5", "5min": "M5",
    "m15": "M15", "15m": "M15", "15min": "M15",
    "m30": "M30", "30m": "M30",
    "h1": "H1", "1h": "H1", "60m": "H1",
}

import re


def _norm_dir(token: str) -> Optional[str]:
    t = token.strip().lower().strip(".,;:!?")
    return DIRECTION_MAP.get(t)


def _norm_tf(token: str) -> Optional[str]:
    t = token.strip().lower().strip(".,;:!?")
    return TF_MAP.get(t)


def _norm_time(token: str) -> Optional[str]:
    m = re.search(r"\b([01]?\d|2[0-3])\s*[:hH]\s*([0-5]\d)\b", token)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    m = re.search(r"\b([01]?\d|2[0-3])\s*[hH]\b", token)
    if m:
        return f"{int(m.group(1)):02d}:00"
    return None


def _norm_active(token: str) -> Optional[str]:
    t = token.strip().upper().strip(".,;:!?")
    # Remove sufixos de emoji/lixo
    t = re.sub(r"[^A-Z0-9\-/_]", "", t)
    t = re.sub(r"-{2,}", "-", t)          # colapsa hifens duplicados
    t = t.strip("-")
    if not t:
        return None
    known = ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "BRL",
             "BTC", "ETH", "XAU", "XAG", "OIL", "SP", "NAS", "DAX")
    if any(k in t for k in known) and len(t) >= 5:
        return t
    return None


def parse_signal_line(line: str) -> Optional[dict]:
    """
    Interpreta UMA linha de sinal em praticamente qualquer formato:
      M1;EURGBP-OTC;21:11 CALL
      EURJPY-OTC 21:11 PUT
      EURJPY OTC 21:11 put M1
      21:11 EURJPY PUT
      M5,GBPUSD-OTC,21:15,venda
      M1 EURJPY-OTC 21:11 COMPRA
    """
    if not line or not line.strip():
        return None
    raw = line.strip()

    # Descarta cabecalhos/ruido comuns de grupos de sinais
    low = raw.lower()
    if any(x in low for x in ("sinal", "entrada", "resultado", "win", "loss",
                              "gale", "atenção", "atencao")) and not _norm_time(raw):
        return None

    # 1) Direcao
    direction = None
    for tok in re.split(r"[\s;,|/]+", raw):
        d = _norm_dir(tok)
        if d:
            direction = d
            break
    if not direction:
        return None

    # 2) Horario
    hora = _norm_time(raw)
    if not hora:
        return None

    # 3) Timeframe
    tf = None
    for tok in re.split(r"[\s;,|/]+", raw):
        t = _norm_tf(tok)
        if t:
            tf = t
            break
    tf = tf or "M1"

    # 4) Ativo: primeiro token "parecido com ativo" que nao seja dir/tf/hora
    active = None
    tokens = re.split(r"[\s;,|/]+", raw)
    for i, tok in enumerate(tokens):
        if _norm_dir(tok) or _norm_tf(tok) or _norm_time(tok):
            continue
        a = _norm_active(tok)
        if not a:
            # Caso "EUR/USD" ou "EUR USD": rejunta o par de moedas
            if (len(tok) == 3 and tok.isalpha() and i + 1 < len(tokens)
                    and len(tokens[i + 1]) == 3 and tokens[i + 1].isalpha()):
                juntado = _norm_active(tok + tokens[i + 1])
                if juntado:
                    a = juntado
                    # Se o proximo-proximo for OTC solto, junta
                    if i + 2 < len(tokens) and tokens[i + 2].strip().upper() == "OTC" and "OTC" not in a:
                        a = f"{a}-OTC"
                    active = a
                    break
            continue
        # Se o token seguinte for "OTC" solto, junta (EURJPY OTC -> EURJPY-OTC)
        if i + 1 < len(tokens) and tokens[i + 1].strip().upper() == "OTC" and "OTC" not in a:
            a = f"{a}-OTC"
        active = a
        break
    if not active:
        return None

    # 6) Normaliza: colapsa hifens duplicados (EURGBP--OTC -> EURGBP-OTC)
    active = re.sub(r"-{2,}", "-", active).strip("-")

    return {"timeframe": tf, "active": active, "time": hora, "direction": direction}


def parse_signal_list(text: str) -> List[dict]:
    """Interpreta um bloco de texto com varios sinais (um por linha)."""
    signals = []
    for line in text.splitlines():
        sig = parse_signal_line(line)
        if sig:
            signals.append(sig)
    return signals


# ============================================================
# ANALISE MULTI-TIMEFRAME (M1 + M5 + M15 alinhados)
# ============================================================

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600}
TF_LABEL = {"M1": "M1", "M5": "M5", "M15": "M15", "M30": "M30", "H1": "H1"}


@dataclass
class MultiTFResult:
    active: str
    per_tf: dict            # {"M1": SignalResult, ...}
    aligned_direction: Optional[str]
    aligned_strength: int
    dominant_trend: str
    news_blocked: bool = False
    news_reason: str = ""

    @property
    def has_signal(self) -> bool:
        return self.aligned_direction is not None


def analyze_multi_timeframe(
    candles_by_tf: dict,
    active: str,
    timeframes=("M1", "M5", "M15"),
    min_strength: int = 60,
    check_news: bool = True,
    moment: Optional[datetime] = None,
) -> MultiTFResult:
    """
    Analisa o mesmo ativo em varios timeframes e exige ALINHAMENTO:
    o sinal so vale se pelo menos 2 dos 3 timeframes concordarem na direcao
    (com o timeframe maior tendo peso de desempate).
    """
    per_tf: dict = {}
    for tf in timeframes:
        candles = candles_by_tf.get(tf) or []
        per_tf[tf] = generate_signal(
            candles, active,
            min_strength=min_strength, check_news=check_news, moment=moment,
        )

    # Votos com peso: timeframes maiores pesam mais
    weight = {"M1": 1, "M5": 2, "M15": 3, "M30": 4, "H1": 5}
    votes = {"call": 0, "put": 0}
    news_blocked = False
    news_reason = ""
    for tf, res in per_tf.items():
        if res.blocked_by and "oticia" in res.blocked_by:
            news_blocked = True
            news_reason = res.blocked_by
        if res.direction:
            votes[res.direction] += weight.get(tf, 1)

    total_votes = votes["call"] + votes["put"]
    if total_votes == 0:
        aligned_dir = None
        strength = max((r.strength for r in per_tf.values()), default=0)
    else:
        aligned_dir = "call" if votes["call"] > votes["put"] else "put"
        dominant = votes[aligned_dir] / total_votes
        # forca = media das forcas na direcao escolhida * alinhamento
        dir_strengths = [r.strength for r in per_tf.values() if r.direction == aligned_dir]
        avg = sum(dir_strengths) / len(dir_strengths) if dir_strengths else 0
        strength = int(round(avg * dominant))

    # Tendencia dominante (do timeframe maior)
    biggest = max(timeframes, key=lambda t: weight.get(t, 1))
    dominant_trend = per_tf[biggest].trend if biggest in per_tf else "lateral"

    return MultiTFResult(
        active=active,
        per_tf=per_tf,
        aligned_direction=aligned_dir,
        aligned_strength=strength,
        dominant_trend=dominant_trend,
        news_blocked=news_blocked,
        news_reason=news_reason,
    )


# ============================================================
# CHAT POR REGRAS (interpreta pedidos em linguagem simples)
# ============================================================

_SCAN_VERBS = ("analisa", "analisar", "analise", "varre", "varrer", "varredura", "scan",
               "verifica", "checar", "checa", "olha", "ve", "ver", "estuda",
               "avalia", "como esta", "como ta", "sinal", "entrada", "setup", "oportunidade",
               "pesquisa", "busca", "buscar", "procura", "acha", "previsao", "prever",
               "preve", "perspectiva", "cenario", "tendencia", "o que acha", "qual a",
               "ta como", "vale")
_SIGNAL_VERBS = ("tem sinal", "algum sinal", "tem entrada", "ta bom", "esta bom",
                 "vale a pena", "ta valendo", "melhor sinal", "melhores sinais")


def detect_scan_request(text: str) -> Optional[str]:
    """
    Se a mensagem for um pedido de analise, devolve o ATIVO a analisar.
    Caso contrario, devolve None.

    Exemplos que funcionam:
      analisa EURUSD
      varre o EURJPY-OTC
      como esta o GBPUSD hoje?
      tem sinal no BTCUSD?
    """
    if not text:
        return None
    low = text.lower().strip()

    tem_verbo = any(v in low for v in _SCAN_VERBS) or any(v in low for v in _SIGNAL_VERBS)
    if not tem_verbo:
        return None

    # Procura um ativo na mensagem
    toks = re.split(r"[\s;,|/?!.]+", text)
    for i, tok in enumerate(toks):
        if _norm_dir(tok) or _norm_tf(tok) or _norm_time(tok):
            continue
        a = _norm_active(tok)
        # Rejunta par de moedas separado por barra/espaço: "EUR USD" -> EURUSD
        if not a and len(tok) == 3 and tok.isalpha() and i + 1 < len(toks) \
                and len(toks[i + 1]) == 3 and toks[i + 1].isalpha():
            a = _norm_active(tok + toks[i + 1])
        if a:
            return re.sub(r"-{2,}", "-", a)
    return None


def detect_generic_signal_request(text: str) -> bool:
    """Detecta pedido generico sem ativo: 'tem sinal agora?', 'melhores sinais'."""
    if not text:
        return False
    low = text.lower().strip()
    return any(v in low for v in _SIGNAL_VERBS)
