import os
import sys
import asyncio
import logging
import json
import time
import threading
import ssl
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

os.environ['SSL_CERT_FILE'] = ''

# Stats globais por usuario
user_stats = {}

def safe_check_win(api, order_id, timeout=90):
    """Verifica resultado com timeout - nao trava"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            if hasattr(api, 'socket_option_closed') and order_id in api.socket_option_closed:
                x = api.socket_option_closed[order_id]
                if x is not None:
                    win = x['msg']['win']
                    if win == 'equal':
                        return 0
                    elif win == 'loose':
                        return -float(x['msg']['sum'])
                    else:
                        return float(x['msg']['win_amount']) - float(x['msg']['sum'])
        except:
            pass
        time.sleep(1)
    return None
ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv()
TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger('Bot')

licenses = {}
admins_set = set()
user_states = {}

# Diretorio de dados (JSONs de config/sinais/grupos)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "bot_config.json")

def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

def is_admin(uid):
    return uid == ADMIN_ID or uid in admins_set

def is_license_valid(uid):
    if uid == ADMIN_ID: return True
    return licenses.get(uid, 0) > time.time()

def set_license(uid, days):
    licenses[uid] = int(time.time()) + (days * 86400)

# ============================================================
# EXNOVA API
# ============================================================

exnova_instances = {}

def get_exnova(user_id):
    return exnova_instances.get(user_id)

def connect_exnova(user_id, email, password):
    try:
        from exnovaapi.stable_api import Exnova
        api = Exnova(email, password)
        status, msg = api.connect()
        if status:
            api._login_email = email
            exnova_instances[user_id] = api
            return True, "Conectado!"
        return False, msg
    except Exception as e:
        return False, str(e)

def disconnect_exnova(user_id):
    api = exnova_instances.pop(user_id, None)
    if api:
        try:
            api.logout()
        except:
            pass
    return True

# ============================================================
# INDICADOR - LEVERAGE INVEST
# Continuacao: 3 velas + pullback + rompimento
# ============================================================

def check_signal(candles):
    """
    candles = lista de velas [{open, high, low, close}, ...]
    Retorna: 'call', 'put' ou None
    """
    if len(candles) < 4:
        return None

    c = candles
    verde_3 = c[-4]['close'] > c[-4]['open']
    verde_2 = c[-3]['close'] > c[-3]['open']
    verde_1 = c[-2]['close'] > c[-2]['open']
    verde_0 = c[-1]['close'] > c[-1]['open']

    vermelha_3 = c[-4]['close'] < c[-4]['open']
    vermelha_2 = c[-3]['close'] < c[-3]['open']
    vermelha_1 = c[-2]['close'] < c[-2]['open']
    vermelha_0 = c[-1]['close'] < c[-1]['open']

    pavio_inferior = c[-1]['low'] < min(c[-1]['open'], c[-1]['close'])
    pavio_superior = c[-1]['high'] > max(c[-1]['open'], c[-1]['close'])

    rompe_max = c[-1]['high'] > c[-2]['high']
    rompe_min = c[-1]['low'] < c[-2]['low']

    # COMPRA: 3 verdes + 1 vermelha + 1 verde + rompe max + pavio inferior
    if verde_3 and verde_2 and vermelha_1 and verde_0 and rompe_max and pavio_inferior:
        return 'call'

    # VENDA: 3 vermelhas + 1 verde + 1 vermelha + rompe min + pavio superior
    if vermelha_3 and vermelha_2 and verde_1 and vermelha_0 and rompe_min and pavio_superior:
        return 'put'

    return None

# ============================================================
# TRADING AUTOMATICO
# ============================================================

class AutoTrader:
    def __init__(self, user_id, bot, loop):
        self.user_id = user_id
        self.bot = bot
        self.loop = loop
        self.running = True
        self.stats = {"wins": 0, "losses": 0, "total": 0, "profit": 0}

        # Ler config salva
        cfg = load_config().get(str(user_id), {})
        self.active = cfg.get("active", "EURUSD-OTC")
        self.amount = cfg.get("amount", 1.0)
        self.expiration = cfg.get("expiration", 1)
        self.current_amount = self.amount

        # Martingale
        self.martingale_enabled = cfg.get("martingale", True)
        self.martingale_multiplier = cfg.get("mart_mult", 2.0)
        self.martingale_max_steps = cfg.get("mart_steps", 3)

        # Soros Gale
        self.soros_enabled = cfg.get("soros", True)
        self.soros_multiplier = cfg.get("soros_mult", 1.5)
        self.soros_max_steps = cfg.get("soros_steps", 3)

        # Ciclos
        self.max_cycles = cfg.get("cycles", 0)

        self.martingale_step = 0
        self.soros_step = 0
        self.last_result = None

    def send_msg(self, text):
        try:
            asyncio.run_coroutine_threadsafe(
                self.bot.send_message(chat_id=self.user_id, text=text),
                self.loop
            )
        except:
            pass

    def get_next_amount(self):
        if self.last_result == "loss" and self.martingale_enabled and self.martingale_step < self.martingale_max_steps:
            self.martingale_step += 1
            self.soros_step = 0
            return self.amount * (self.martingale_multiplier ** self.martingale_step)
        elif self.last_result == "win" and self.soros_enabled and self.soros_step < self.soros_max_steps:
            self.soros_step += 1
            self.martingale_step = 0
            return self.amount * (self.soros_multiplier ** self.soros_step)
        else:
            self.martingale_step = 0
            self.soros_step = 0
            return self.amount

    def run_loop(self):
        import time as _time
        api = get_exnova(self.user_id)
        if not api:
            self.send_msg("❌ Nao conectado na EXNOVA!")
            return

        # Carregar sinais se existir
        signal_file = os.path.join(DATA_DIR, f"signals_{self.user_id}.json")
        signal_list = None
        if os.path.exists(signal_file):
            with open(signal_file, 'r') as f:
                signal_list = json.load(f)
            self.send_msg(
                f"🤖 AUTO TRADE INICIADO!\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Modo: LISTA DE SINAIS\n"
                f"Total: {len(signal_list)} sinais\n"
                f"Martingale: {'ON' if self.martingale_enabled else 'OFF'}\n"
                f"Soros Gale: {'ON' if self.soros_enabled else 'OFF'}"
            )
        else:
            self.send_msg(
                f"🤖 AUTO TRADE INICIADO!\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Modo: INDICADOR AUTOMATICO\n"
                f"Ativo: {self.active}\n"
                f"Valor base: ${self.amount:.2f}\n"
                f"Expiracao: {self.expiration}min\n"
                f"Martingale: {'ON' if self.martingale_enabled else 'OFF'}\n"
                f"Soros Gale: {'ON' if self.soros_enabled else 'OFF'}"
            )

        last_signal = None
        signal_index = 0
        executed_times = set()

        while self.running:
            try:
                api = get_exnova(self.user_id)
                if not api:
                    break

                # Modo LISTA DE SINAIS
                if signal_list:
                    now = datetime.now()
                    current_time = now.strftime("%H:%M")
                    current_min = now.hour * 60 + now.minute

                    for i, sig in enumerate(signal_list):
                        if signal_index > i:
                            continue

                        sig_time = sig['time']
                        time_key = f"{sig_time}_{i}"

                        # Calcular minutos do sinal
                        parts = sig_time.split(":")
                        sig_min = int(parts[0]) * 60 + int(parts[1])

                        # Executar se horario bateu (com tolerancia de 2 min) e ainda nao executou
                        if abs(current_min - sig_min) <= 2 and time_key not in executed_times:
                            trade_amount = self.get_next_amount()
                            trade_amount = round(trade_amount, 2)
                            direction = sig['direction'].lower()

                            # Converter timeframe para minutos de expiracao
                            tf = sig.get('timeframe', 'M1')
                            tf_map = {"M1": 1, "M2": 2, "M5": 5, "M15": 15, "M30": 30, "H1": 60}
                            exp_mins = tf_map.get(tf, 1)

                            # Step info
                            step_info = ""
                            if self.martingale_step > 0:
                                step_info = f"🔴 Martingale Step: {self.martingale_step}\n"
                            elif self.soros_step > 0:
                                step_info = f"🟢 Soros Step: {self.soros_step}\n"

                            self.send_msg(
                                f"⏰ Sinal agendado para agora!\n"
                                f"💬 Ativo: {sig['active']}\n"
                                f" Dir: {direction.upper()}\n"
                                f"⏳ Expiracao: {tf} ({exp_mins} min)\n"
                                f"⚙️ Abrindo ordem..."
                            )

                            status = False
                            order_id = ""
                            try:
                                status, order_id = api.buy(trade_amount, sig['active'], direction, exp_mins)
                            except KeyError:
                                self.send_msg(f"❌ Ativo {sig['active']} nao encontrado na EXNOVA!\nUse /ativos pra ver lista")
                                executed_times.add(time_key)
                                break
                            except Exception as e:
                                self.send_msg(f"❌ Erro ao comprar {sig['active']}: {e}")
                                executed_times.add(time_key)
                                break
                            if status:
                                self.stats["total"] += 1
                                executed_times.add(time_key)
                                self.send_msg(
                                    f"📩 Ordem Executada!\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                    f"💬 Ativo: {sig['active']}\n"
                                    f"📊 Entrada: R${trade_amount:.2f}\n"
                                    f" Dir: {direction.upper()}\n"
                                    f"⏳ Expiracao: {tf} ({exp_mins} min)\n"
                                    f"{step_info}"
                                    f"🔀 Opcoes: DIGITAL\n"
                                    f"🆔 Order: {order_id}\n"
                                    f"⏳ Aguardando resultado..."
                                )
                                signal_index = i + 1

                                _time.sleep(exp_mins * 60 + 10)

                                result = safe_check_win(api, order_id, timeout=60)
                                if result is not None:
                                    if result > 0:
                                        self.stats["wins"] += 1
                                        self.stats["profit"] += result
                                        self.last_result = "win"
                                        self.martingale_step = 0
                                        self.soros_step = 0
                                        self.send_msg(
                                            f"🤑✅ WIN !\n"
                                            f"━━━━━━━━━━━━━━━━━━\n"
                                            f"💬 Ativo: {sig['active']}\n"
                                            f"📊 Entrada: R${trade_amount:.2f}\n"
                                            f"💎 Lucro liquido: R${result:.2f}\n"
                                            f"🔀 Opcoes: DIGITAL\n"
                                            f"➡️ Resultado: WIN\n"
                                            f"━━━━━━━━━━━━━━━━━━\n"
                                            f"📊 Placar: {self.stats['wins']}W x {self.stats['losses']}L\n"
                                            f"💰 Lucro total: R${self.stats['profit']:.2f}"
                                        )
                                    else:
                                        self.stats["losses"] += 1
                                        self.stats["profit"] -= abs(result)
                                        self.last_result = "loss"
                                        self.send_msg(
                                            f"🤑❌ LOSS !\n"
                                            f"━━━━━━━━━━━━━━━━━━\n"
                                            f"💬 Ativo: {sig['active']}\n"
                                            f"📊 Entrada: R${trade_amount:.2f}\n"
                                            f"💎 Perda: R${abs(result):.2f}\n"
                                            f"🔀 Opcoes: DIGITAL\n"
                                            f"➡️ Resultado: LOSS\n"
                                            f"━━━━━━━━━━━━━━━━━━\n"
                                            f"📊 Placar: {self.stats['wins']}W x {self.stats['losses']}L\n"
                                            f"💰 Lucro total: R${self.stats['profit']:.2f}"
                                        )
                                else:
                                    balance = api.get_balance()
                                    self.send_msg(
                                        f"⏳ Operacao finalizada (timeout)\n"
                                        f"💬 Ativo: {sig['active']}\n"
                                        f"💰 Saldo: R${balance:.2f}\n"
                                        f"⚠️ Verifique na corretora"
                                    )
                            else:
                                self.send_msg(f"Erro ao abrir: {order_id}")
                                executed_times.add(time_key)
                            break

                    _time.sleep(2)

                # Modo INDICADOR AUTOMATICO
                else:
                    candles = api.get_candles(self.active, 60, 5, int(time.time()))
                    if not candles or len(candles) < 5:
                        _time.sleep(5)
                        continue

                    candle_data = []
                    for candle in candles:
                        candle_data.append({
                            'open': candle['open'],
                            'high': candle['high'],
                            'low': candle['low'],
                            'close': candle['close']
                        })

                    signal = check_signal(candle_data)

                    if signal and signal != last_signal:
                        trade_amount = self.get_next_amount()
                        trade_amount = round(trade_amount, 2)

                        # Step info
                        step_info = ""
                        if self.martingale_step > 0:
                            step_info = f"\n🔴 Martingale Step: {self.martingale_step}"
                        elif self.soros_step > 0:
                            step_info = f"\n🟢 Soros Step: {self.soros_step}"

                        status, order_id = api.buy(trade_amount, self.active, signal, self.expiration)
                        if status:
                            self.stats["total"] += 1
                            emoji = "🟢" if signal == "call" else "🔴"

                            self.send_msg(
                                f"📩 Ordem Executada!\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"💬 Ativo: {self.active}\n"
                                f"📊 Entrada: R${trade_amount:.2f}\n"
                                f" Dir: {signal.upper()}\n"
                                f"⏳ Expiracao: {self.expiration} min\n"
                                f"{step_info}\n"
                                f"🔀 Opcoes: DIGITAL\n"
                                f"🆔 Order: {order_id}\n"
                                f"⏳ Aguardando resultado..."
                            )

                            _time.sleep(self.expiration * 60 + 10)

                            result = safe_check_win(api, order_id, timeout=60)
                            if result is not None:
                                if result > 0:
                                    self.stats["wins"] += 1
                                    self.stats["profit"] += result
                                    self.last_result = "win"
                                    self.martingale_step = 0
                                    self.soros_step = 0
                                    self.send_msg(
                                        f"🤑✅ WIN !\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"💬 Ativo: {self.active}\n"
                                        f"📊 Entrada: R${trade_amount:.2f}\n"
                                        f"💎 Lucro liquido: R${result:.2f}\n"
                                        f"🔀 Opcoes: DIGITAL\n"
                                        f"➡️ Resultado: WIN\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"📊 Placar: {self.stats['wins']}W x {self.stats['losses']}L\n"
                                        f"💰 Lucro total: R${self.stats['profit']:.2f}"
                                    )
                                else:
                                    self.stats["losses"] += 1
                                    self.stats["profit"] -= abs(result)
                                    self.last_result = "loss"
                                    self.send_msg(
                                        f"🤑❌ LOSS !\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"💬 Ativo: {self.active}\n"
                                        f"📊 Entrada: R${trade_amount:.2f}\n"
                                        f"💎 Perda: R${abs(result):.2f}\n"
                                        f"🔀 Opcoes: DIGITAL\n"
                                        f"➡️ Resultado: LOSS\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"📊 Placar: {self.stats['wins']}W x {self.stats['losses']}L\n"
                                        f"💰 Lucro total: R${self.stats['profit']:.2f}"
                                    )
                            else:
                                balance = api.get_balance()
                                self.send_msg(
                                    f"⏳ Operacao finalizada (timeout)\n"
                                    f"💬 Ativo: {self.active}\n"
                                    f"💰 Saldo: R${balance:.2f}\n"
                                    f"⚠️ Verifique na corretora"
                                )

                            last_signal = signal
                        else:
                            self.send_msg(f"Erro: {order_id}")
                    elif signal == last_signal:
                        pass
                    else:
                        last_signal = None

            except Exception as e:
                log.error("AutoTrader error: %s", e)

            _time.sleep(5)

        self.send_msg(
            f"🤖 AUTO TRADER PARADO!\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Total: {self.stats['total']} operacoes\n"
            f"Wins: {self.stats['wins']} | Losses: {self.stats['losses']}\n"
            f"Profit: ${self.stats['profit']:.2f}"
        )

# ============================================================
# MONITOR DE SINAIS AO VIVO
# ============================================================

class SignalMonitor:
    def __init__(self, user_id, bot, loop, groups):
        self.user_id = user_id
        self.bot = bot
        self.loop = loop
        self.groups = groups
        self.running = True
        self.last_messages = {}

    def send_msg(self, text):
        try:
            asyncio.run_coroutine_threadsafe(
                self.bot.send_message(chat_id=self.user_id, text=text),
                self.loop
            )
        except:
            pass

    def parse_signal(self, text):
        text_upper = text.upper().strip()
        lines = text.split('\n')

        direction = None
        asset = None
        timeframe = None
        entry_time = None
        signal_type = "DIGITAL"

        for line in lines:
            lu = line.upper().strip()

            # === ATIVO ===
            if "ATIVO" in lu:
                raw = lu.split(":", 1)[1] if ":" in lu else ""
                # Limpar emojis e labels
                for cut in ["🎯", "DIGITAL", "BINARY", "|", "✍", "🗣", "⏰", "⏳", "📊"]:
                    cut_u = cut.upper() if cut.isascii() else cut
                    raw = raw.replace(cut_u, "").replace(cut, "")
                raw = raw.strip()
                # GBP/USD (OTC) -> GBPUSD-OTC
                raw = raw.replace("/", "").replace("(", "").replace(")", "").strip()
                raw = ''.join(c for c in raw if c.isascii())
                if raw:
                    words = raw.split()
                    # Pegar tudo que nao e lixo
                    asset_words = []
                    for w in words:
                        if w not in ["DIGITAL", "BINARY", "OTC", ""] and len(w) > 1:
                            asset_words.append(w)
                    if asset_words:
                        asset = " ".join(asset_words)
                        if "OTC" in lu and "-OTC" not in asset:
                            asset = asset + "-OTC"

            # === EXPIRACAO ===
            if "EXPIRA" in lu:
                raw = lu.split(":", 1)[1] if ":" in lu else ""
                for t in ["M1", "M2", "M5", "M15", "M30", "H1"]:
                    if t in raw:
                        timeframe = t
                        break

            # === DIRECAO ===
            if "DIREÇÃO" in lu or "DIRECAO" in lu:
                raw = lu.split(":", 1)[1] if ":" in lu else ""
                if "COMPRA" in raw or "CALL" in raw or "CIMA" in raw or "BUY" in raw or "ALTA" in raw:
                    direction = "call"
                elif "VENDA" in raw or "PUT" in raw or "BAIXO" in raw or "SELL" in raw or "BAIXA" in raw:
                    direction = "put"

            # === ENTRADA (hora) ===
            if "ENTRADA" in lu or "HORA" in lu:
                raw = lu.split(":", 1)[1] if ":" in lu else ""
                raw = raw.strip().split()[0].strip() if raw.strip() else ""
                if ":" in raw or len(raw) == 4:
                    entry_time = raw.strip()

            # === TIPO ===
            if "DIGITAL" in lu and "ATIVO" not in lu:
                signal_type = "DIGITAL"
            elif "BINARIA" in lu or "BINÁRIA" in lu or "BINARY" in lu:
                signal_type = "BINARY"

        # === FALLBACKS ===

        # Fallback: COMPRA/VENDA solto em qualquer linha
        if not direction:
            for line in lines:
                lu = line.upper().strip()
                if "COMPRA" in lu or "CALL" in lu or "BUY" in lu or "ALTA" in lu:
                    direction = "call"
                    break
                elif "VENDA" in lu or "PUT" in lu or "SELL" in lu or "BAIXA" in lu:
                    direction = "put"
                    break

        # Fallback: ativo com OTC em qualquer lugar
        if not asset:
            for line in lines:
                lu = line.upper().strip()
                if "OTC" in lu:
                    raw = lu.replace("/", "").replace("(", "").replace(")", "").replace("|", "").replace("🎯", "").strip()
                    raw = raw.replace("DIGITAL", "").replace("BINARY", "").strip()
                    raw = ''.join(c for c in raw if c.isascii())
                    words = raw.split()
                    asset_words = []
                    for w in words:
                        if w not in ["OTC", "DIGITAL", "BINARY", ""] and len(w) > 1:
                            asset_words.append(w)
                    if asset_words:
                        asset = asset_words[-1] + "-OTC" if "OTC" in lu else asset_words[-1]
                        break

        # Fallback: timeframe
        if not timeframe:
            for line in lines:
                for w in line.upper().split():
                    for t in ["M1", "M2", "M5", "M15", "M30", "H1"]:
                        if t == w or t in w:
                            timeframe = t
                            break
                    if timeframe:
                        break
                if timeframe:
                    break

        if direction and asset:
            return {
                "direction": direction,
                "active": asset,
                "timeframe": timeframe or "M1",
                "entry_time": entry_time,
                "type": signal_type
            }
        return None

    def run_loop(self):
        import time as _time
        api = get_exnova(self.user_id)
        if not api:
            self.send_msg("❌ Nao conectado na EXNOVA!")
            return

        self.send_msg(
            f"📡 MONITOR DE SINAIS INICIADO!\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Monitorando {len(self.groups)} grupo(s)\n"
            f"Aguardando sinais..."
        )

        cfg = load_config().get(str(self.user_id), {})
        amount = cfg.get("amount", 1.0)
        expiration = cfg.get("expiration", 1)

        while self.running:
            try:
                _time.sleep(10)
            except:
                pass

        self.send_msg("📡 Monitor PARADO!")

# ============================================================
# COPYTRADER
# ============================================================

copytraders = {}

class CopyTrader:
    def __init__(self, user_id, bot, loop, master_id, follower_id):
        self.user_id = user_id
        self.bot = bot
        self.loop = loop
        self.master_id = master_id
        self.follower_id = follower_id
        self.running = True
        self.last_trades = []
        self.stats = {"wins": 0, "losses": 0, "total": 0, "profit": 0}

    def send_msg(self, text):
        try:
            asyncio.run_coroutine_threadsafe(
                self.bot.send_message(chat_id=self.user_id, text=text),
                self.loop
            )
        except:
            pass

    def run_loop(self):
        import time as _time
        master_api = get_exnova(self.master_id)
        follower_api = get_exnova(self.follower_id)

        if not master_api or not follower_api:
            self.send_msg("❌ Contas master ou follower nao conectadas!")
            return

        cfg = load_config().get(str(self.user_id), {})
        amount = cfg.get("amount", 1.0)

        self.send_msg(
            f"📋 COPYTRADER INICIADO!\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Master: {self.master_id}\n"
            f"Follower: {self.follower_id}\n"
            f"Valor: R${amount:.2f}"
        )

        checked_ids = set()

        while self.running:
            try:
                master_api = get_exnova(self.master_id)
                follower_api = get_exnova(self.follower_id)

                if not master_api or not follower_api:
                    _time.sleep(5)
                    continue

                try:
                    history = master_api.get_trades(0, 10)
                except:
                    history = []

                if history:
                    for trade in history:
                        trade_id = trade.get("id", "")
                        if trade_id in checked_ids:
                            continue

                        now = time.time()
                        trade_time = trade.get("timestamp", 0) / 1000 if trade.get("timestamp", 0) > 1e10 else trade.get("timestamp", 0)

                        if now - trade_time > 300:
                            continue

                        direction = trade.get("direction", "").lower()
                        active = trade.get("instrument", "")
                        win = trade.get("win", False)
                        profit = trade.get("profit", 0)
                        invest = trade.get("amount", amount)

                        if not direction or not active:
                            continue

                        checked_ids.add(trade_id)

                        emoji = "🟢" if direction == "call" else "🔴"
                        status, order_id = follower_api.buy(invest, active, direction, 1)

                        if status:
                            self.stats["total"] += 1
                            if win:
                                self.stats["wins"] += 1
                                self.stats["profit"] += profit
                                self.send_msg(
                                    f"📋 COPYTRADER - WIN!\n"
                                    f"💬 Ativo: {active}\n"
                                    f"📊 Entrada: R${invest:.2f}\n"
                                    f"💎 Lucro: R${profit:.2f}\n"
                                    f"💰 Lucro total: R${self.stats['profit']:.2f}\n"
                                    f"➡️ Placar: ✅ {self.stats['wins']} x {self.stats['losses']} ❌"
                                )
                            else:
                                self.stats["losses"] += 1
                                self.stats["profit"] -= abs(profit)
                                self.send_msg(
                                    f"📋 COPYTRADER - LOSS\n"
                                    f"💬 Ativo: {active}\n"
                                    f"📊 Entrada: R${invest:.2f}\n"
                                    f"💎 Perda: R${abs(profit):.2f}\n"
                                    f"💰 Lucro total: R${self.stats['profit']:.2f}\n"
                                    f"➡️ Placar: ✅ {self.stats['wins']} x {self.stats['losses']} ❌"
                                )

            except Exception as e:
                log.error("CopyTrader error: %s", e)

            _time.sleep(5)

        self.send_msg(
            f"📋 COPYTRADER PARADO!\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Total: {self.stats['total']}\n"
            f"Wins: {self.stats['wins']} | Losses: {self.stats['losses']}\n"
            f"Profit: R${self.stats['profit']:.2f}"
        )

# ============================================================
# HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_license_valid(uid) and not is_admin(uid):
        await update.message.reply_text("LICENCA EXPIRADA!")
        return

    # Enviar logo
    logo_path = os.path.join(os.path.dirname(__file__), "logo.png")
    if os.path.exists(logo_path):
        with open(logo_path, "rb") as logo:
            await update.message.reply_photo(
                photo=logo,
                caption=(
                    "🚀 *BEM-VINDO AO LEVERAGE INVEST!*\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    "🤖 *Seu Robô de Trading Inteligente*\n\n"
                    "📊 Opções Digitais na EXNOVA\n"
                    "⚡ Execução automática e manual\n"
                    "📈 Indicador LEVERAGE INVEST\n"
                    "🎰 Gerenciamento: Martingale, Soros e Ciclos\n"
                    "📋 Lista de sinais integrada\n\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "💰 Plataforma 100% automatizada\n"
                    "🔒 Seguro e rápido\n"
                    "📱 Acesse pelo celular ou PC\n\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "⚡ *Comece agora mesmo!*"
                ),
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            "🚀 *BEM-VINDO AO LEVERAGE INVEST!*\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🤖 *Seu Robô de Trading Inteligente*\n\n"
            "📊 Opções Digitais na EXNOVA\n"
            "⚡ Execução automática e manual\n"
            "📈 Indicador LEVERAGE INVEST\n"
            "🎰 Gerenciamento: Martingale, Soros e Ciclos\n"
            "📋 Lista de sinais integrada\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💰 Plataforma 100% automatizada\n"
            "🔒 Seguro e rápido\n"
            "📱 Acesse pelo celular ou PC\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⚡ *Comece agora mesmo!*",
            parse_mode="Markdown"
        )

    api = get_exnova(uid)
    status = "🟢 Conectado" if api else "🔴 Desconectado"
    kb = [
        [KeyboardButton("📊 Status"), KeyboardButton("🔌 Conectar")],
        [KeyboardButton("📈 Operar"), KeyboardButton("🤖 Auto Trade")],
        [KeyboardButton("📡 Sinais Ao Vivo"), KeyboardButton("📋 Copytrader")],
        [KeyboardButton("⚙️ Config"), KeyboardButton("👑 Admin")],
    ]
    reply_markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
    await update.message.reply_text(
        f"EXNOVA: {status}\n\n"
        f"Escolha uma opcao:",
        reply_markup=reply_markup
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    api = get_exnova(uid)
    if not api:
        await update.message.reply_text("❌ Nao conectado na EXNOVA!\nUse 🔌 Conectar")
        return
    try:
        balance = api.get_balance()
        mode = api.get_balance_mode()
        currency = api.get_currency()
        tipo = "🟢 DEMO" if mode == "PRACTICE" else "🔴 REAL"
        running = uid in robots_running
        msg = (
            f"📊 *STATUS EXNOVA*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📡 Tipo: {tipo}\n"
            f"💰 Saldo: *{currency} {balance:.2f}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Auto Trade: {'✅ RODANDO' if running else '⏹ PARADO'}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")

async def cmd_conectar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = {"action": "exnova_email"}
    await update.message.reply_text("🔌 *CONECTAR EXNOVA*\n\nDigite seu email:", parse_mode="Markdown")

async def cmd_operar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    api = get_exnova(uid)
    if not api:
        await update.message.reply_text("❌ Conecte na EXNOVA primeiro!")
        return
    kb = [
        [KeyboardButton("🟢 CALL"), KeyboardButton("🔴 PUT")],
        [KeyboardButton("🔄 Mudar conta"), KeyboardButton("📋 Ativos")],
        [KeyboardButton("🔙 Menu")],
    ]
    await update.message.reply_text(
        "📈 *OPERAR*\n━━━━━━━━━━━━━━━━━━\n\nEscolha a direcao:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

async def cmd_auto_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    api = get_exnova(uid)
    if not api:
        await update.message.reply_text("❌ Conecte na EXNOVA primeiro!")
        return
    kb = [
        [KeyboardButton("▶️ Iniciar Auto"), KeyboardButton("⏹ Parar Auto")],
        [KeyboardButton("⚙️ Config Auto"), KeyboardButton("📋 Lista Sinais")],
        [KeyboardButton("🎰 Gerenciamento")],
        [KeyboardButton("🔙 Menu")],
    ]
    running = uid in robots_running
    await update.message.reply_text(
        f"🤖 *AUTO TRADE*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"Status: {'✅ RODANDO' if running else '⏹ PARADO'}\n"
        f"Indicador: LEVERAGE INVEST\n"
        f"Logica: Continuacao 3+1+1",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cfg = load_config().get(str(uid), {})
    await update.message.reply_text(
        f"⚙️ *CONFIGURACOES*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"Ativo: {cfg.get('active', 'EURUSD-OTC')}\n"
        f"Valor: ${cfg.get('amount', 1):.2f}\n"
        f"Expiracao: {cfg.get('expiration', 1)} min",
        parse_mode="Markdown"
    )

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Acesso negado!")
        return
    kb = [
        [KeyboardButton("/admin_license"), KeyboardButton("/admin_addadmin")],
        [KeyboardButton("/start")],
    ]
    await update.message.reply_text("ADMIN", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def cmd_admin_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = {"action": "admin_license"}
    await update.message.reply_text("ID DIAS (ex: 123456 30):")

async def cmd_admin_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = {"action": "admin_addadmin"}
    await update.message.reply_text("ID do novo admin:")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = {}
    await update.message.reply_text("Cancelado.")

robots_running = {}
signal_monitors = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat = update.effective_chat

    # MONITOR DE SINAIS - Grupo Telegram
    if chat.type in ["group", "supergroup"]:
        chat_id = str(chat.id)
        chat_title = chat.title or "Desconhecido"

        # Logar todos os grupos que o bot esta
        all_groups_file = os.path.join(DATA_DIR, "all_groups.json")
        try:
            with open(all_groups_file, 'r') as f:
                all_groups = json.load(f)
        except:
            all_groups = {}
        all_groups[chat_id] = chat_title
        with open(all_groups_file, 'w') as f:
            json.dump(all_groups, f, indent=2)

        for user_id, monitor in signal_monitors.items():
            if monitor.running:
                matched = False
                for g in monitor.groups:
                    if str(g) == chat_id or str(g) == chat_title.lower():
                        matched = True
                        break
                if matched:
                    text_msg = update.message.text or update.message.caption or ""
                    log.info("SINAL RECEBIDO de %s: %s", chat_title, text_msg[:100])
                    signal = monitor.parse_signal(text_msg)
                    if signal:
                        cfg = load_config().get(str(user_id), {})
                        amount = cfg.get("amount", 1.0)
                        expiration = cfg.get("expiration", 1)
                        api = get_exnova(user_id)
                        if api:
                            status, order_id = api.buy(amount, signal["active"], signal["direction"], expiration)
                            emoji = "🟢" if signal["direction"] == "call" else "🔴"
                            if status:
                                asyncio.run_coroutine_threadsafe(
                                    context.bot.send_message(
                                        chat_id=user_id,
                                        text=(
                                            f"📡 SINAL DETECTADO!\n"
                                            f"━━━━━━━━━━━━━━━━━━\n"
                                            f"📩 Entrada: R${amount:.2f} | {signal['active']} | {signal['direction'].upper()} | {signal['timeframe']} | {emoji}\n"
                                            f"Grupo: {chat_title}\n"
                                            f"Aguarde resultado..."
                                        )
                                    ),
                                    asyncio.get_running_loop()
                                )
                            else:
                                asyncio.run_coroutine_threadsafe(
                                    context.bot.send_message(
                                        chat_id=user_id,
                                        text=f"Erro ao abrir ordem: {order_id}"
                                    ),
                                    asyncio.get_running_loop()
                                )
                    else:
                        log.info("Mensagem ignorada (nao e sinal): %s", text_msg[:50])
        return

    if update.message is None or update.message.text is None:
        return

    text = update.message.text.strip()
    state = user_states.get(uid, {})

    # Se tem estado ativo, NAO detectar sinal (deixa o handler de estado processar)
    if not state:
        # Verificar se e sinal (encaminhado, colado, ou monitor rodando)
        temp_parser = SignalMonitor(uid, context.bot, asyncio.get_running_loop(), [])
        signal = temp_parser.parse_signal(text)
        if signal:
            api = get_exnova(uid)
            if not api:
                await update.message.reply_text("❌ Conecte na EXNOVA primeiro!")
                return

            cfg = load_config().get(str(uid), {})
            amount = cfg.get("amount", 1.0)
            trade_amount = amount

            # Gerenciamento
            if uid in signal_monitors and signal_monitors[uid].running:
                auto = AutoTrader.__new__(AutoTrader)
                auto.amount = amount
                auto.martingale_enabled = cfg.get("martingale", True)
                auto.martingale_multiplier = cfg.get("mart_mult", 2.0)
                auto.martingale_max_steps = cfg.get("mart_steps", 3)
                auto.soros_enabled = cfg.get("soros", True)
                auto.soros_multiplier = cfg.get("soros_mult", 1.5)
                auto.soros_max_steps = cfg.get("soros_steps", 3)
                auto.last_result = None
                auto.martingale_step = 0
                auto.soros_step = 0
                trade_amount = auto.get_next_amount()
                trade_amount = round(trade_amount, 2)

            emoji = "🟢" if signal["direction"] == "call" else "🔴"
            tipo = signal.get("type", "DIGITAL")
            entry_time = signal.get("entry_time")

            # Converter timeframe para minutos de expiracao
            tf = signal.get("timeframe", "M1")
            tf_map = {"M1": 1, "M2": 2, "M5": 5, "M15": 15, "M30": 30, "H1": 60}
            expiration = tf_map.get(tf, 1)

            # Aguardar horario de entrada
            if entry_time:
                now = datetime.now()
                try:
                    h, m = map(int, entry_time.split(":"))
                    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    if target > now:
                        wait_secs = int((target - now).total_seconds())
                        await update.message.reply_text(
                            f"📩 SINAL DETECTADO!\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"💬 Ativo: {signal['active']}\n"
                            f"📊 Entrada: R${trade_amount:.2f}\n"
                            f" Dir: {signal['direction'].upper()}\n"
                            f"⏰ Entrada as: {entry_time}\n"
                            f"⏳ Expiracao: {tf} ({expiration} min)\n"
                            f"⏳ Aguardando {wait_secs}s...\n"
                            f"🔀 Opcoes: {tipo}"
                        )
                        import time as _time
                        _time.sleep(wait_secs)
                except:
                    pass

            # Executar ordem
            status, order_id = api.buy(trade_amount, signal["active"], signal["direction"], expiration)
            if status:
                # Calcular step do martingale/ciclo
                step_info = ""
                if uid not in user_stats:
                    user_stats[uid] = {"wins": 0, "losses": 0, "profit": 0, "mart_step": 0, "cycle_step": 0, "base_amount": amount}
                
                stats = user_stats[uid]
                stats["mart_step"] = 0
                stats["cycle_step"] = 0
                
                # Se tem gerenciamento ativo, mostrar step
                if cfg.get("martingale", False) and stats.get("last_result") == "loss":
                    stats["mart_step"] = 1
                    step_info = f"🔴 Martingale Step: 1\n"
                if cfg.get("soros", False) and stats.get("last_result") == "win":
                    stats["soros_step"] = stats.get("soros_step", 0) + 1
                    step_info = f"🟢 Soros Step: {stats['soros_step']}\n"

                await update.message.reply_text(
                    f"📩 Ordem Executada!\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💬 Ativo: {signal['active']}\n"
                    f"📊 Entrada: R${trade_amount:.2f}\n"
                    f" Dir: {signal['direction'].upper()}\n"
                    f"⏳ Expiracao: {tf} ({expiration} min)\n"
                    f"{step_info}"
                    f"🔀 Opcoes: {tipo}\n"
                    f"🆔 Order: {order_id}\n"
                    f"⏳ Aguardando resultado..."
                )

                # Aguardar resultado com timeout seguro
                import time as _time
                _time.sleep(expiration * 60 + 10)

                result = safe_check_win(api, order_id, timeout=60)
                balance = api.get_balance()

                if result is not None:
                    if result > 0:
                        stats["wins"] += 1
                        stats["profit"] += result
                        stats["last_result"] = "win"
                        stats["mart_step"] = 0
                        await update.message.reply_text(
                            f"🤑✅ WIN !\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"💬 Ativo: {signal['active']}\n"
                            f"📊 Entrada: R${trade_amount:.2f}\n"
                            f"💎 Lucro liquido: R${result:.2f}\n"
                            f"💰 Saldo: R${balance:.2f}\n"
                            f"🔀 Opcoes: {tipo}\n"
                            f"➡️ Resultado: WIN\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📊 Placar: {stats['wins']}W x {stats['losses']}L\n"
                            f"💰 Lucro total: R${stats['profit']:.2f}"
                        )
                    else:
                        stats["losses"] += 1
                        stats["profit"] -= abs(result)
                        stats["last_result"] = "loss"
                        await update.message.reply_text(
                            f"🤑❌ LOSS !\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"💬 Ativo: {signal['active']}\n"
                            f"📊 Entrada: R${trade_amount:.2f}\n"
                            f"💎 Perda: R${abs(result):.2f}\n"
                            f"💰 Saldo: R${balance:.2f}\n"
                            f"🔀 Opcoes: {tipo}\n"
                            f"➡️ Resultado: LOSS\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📊 Placar: {stats['wins']}W x {stats['losses']}L\n"
                            f"💰 Lucro total: R${stats['profit']:.2f}"
                        )
                else:
                    await update.message.reply_text(
                        f"⏳ Operacao finalizada (timeout)\n"
                        f"💬 Ativo: {signal['active']}\n"
                        f"💰 Saldo: R${balance:.2f}\n"
                        f"⚠️ Verifique na corretora"
                    )
            else:
                await update.message.reply_text(f"Erro ao abrir ordem: {order_id}")
            return

    if text == "📊 Status":
        await cmd_status(update, context)
        return
    if text == "🔌 Conectar":
        await cmd_conectar(update, context)
        return
    if text == "📈 Operar":
        await cmd_operar(update, context)
        return
    if text == "🤖 Auto Trade":
        await cmd_auto_trade(update, context)
        return
    if text == "⚙️ Config":
        await cmd_config(update, context)
        return
    if text == "👑 Admin":
        await cmd_admin(update, context)
        return
    if text == "🟢 CALL":
        api = get_exnova(uid)
        if api:
            cfg = load_config().get(str(uid), {})
            active = cfg.get('active', 'EURUSD-OTC')
            amount = cfg.get('amount', 1)
            expiration = cfg.get('expiration', 1)
            status, order_id = api.buy(amount, active, "call", expiration)
            if status:
                await update.message.reply_text(
                    f"📩 Realizando entrada (Digital) :\n"
                    f"R${amount:.2f} | {active} | CALL | M{expiration} | 🟢\n"
                    f"Aguarde até que a operação seja finalizada."
                )
            else:
                await update.message.reply_text(f"Erro: {order_id}")
        else:
            await update.message.reply_text("Conecte na EXNOVA primeiro!")
        return
    if text == "🔴 PUT":
        api = get_exnova(uid)
        if api:
            cfg = load_config().get(str(uid), {})
            active = cfg.get('active', 'EURUSD-OTC')
            amount = cfg.get('amount', 1)
            expiration = cfg.get('expiration', 1)
            status, order_id = api.buy(amount, active, "put", expiration)
            if status:
                await update.message.reply_text(
                    f"📩 Realizando entrada (Digital) :\n"
                    f"R${amount:.2f} | {active} | PUT | M{expiration} | 🔴\n"
                    f"Aguarde até que a operação seja finalizada."
                )
            else:
                await update.message.reply_text(f"Erro: {order_id}")
        else:
            await update.message.reply_text("Conecte na EXNOVA primeiro!")
        return
    if text == "▶️ Iniciar Auto":
        if uid in robots_running:
            await update.message.reply_text("Ja esta rodando!")
            return
        trader = AutoTrader(uid, context.bot, asyncio.get_running_loop())
        robots_running[uid] = trader
        threading.Thread(target=trader.run_loop, daemon=True).start()
        await update.message.reply_text("🤖 Auto Trade INICIADO!")
        return
    if text == "⏹ Parar Auto":
        if uid in robots_running:
            robots_running[uid].running = False
            del robots_running[uid]
            await update.message.reply_text("🤖 Auto Trade PARADO!")
        else:
            await update.message.reply_text("Nao esta rodando!")
        return
    if text == "⚙️ Config Auto":
        user_states[uid] = {"action": "cfg_active"}
        await update.message.reply_text("Ativo (ex: EURUSD-OTC):")
        return
    if text == "📋 Copytrader":
        cfg = load_config().get(str(uid), {})
        kb = [
            [KeyboardButton("▶️ Iniciar Copy"), KeyboardButton("⏹ Parar Copy")],
            [KeyboardButton("👤 Config Master"), KeyboardButton("👥 Config Follower")],
            [KeyboardButton("🔙 Voltar")],
        ]
        reply_markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
        master = cfg.get("copy_master", "Nao configurado")
        follower = cfg.get("copy_follower", "Nao configurado")
        running = uid in copytraders
        await update.message.reply_text(
            f"📋 *COPYTRADER*\n━━━━━━━━━━━━━━━━━━\n\n"
            f"Status: {'✅ RODANDO' if running else '⏹ PARADO'}\n"
            f"Master: {master}\n"
            f"Follower: {follower}\n\n"
            f"Copia trades automaticamente!",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return
    if text == "👤 Config Master":
        user_states[uid] = {"action": "copy_master"}
        await update.message.reply_text(
            "👤 *CONFIGURAR MASTER*\n━━━━━━━━━━━━━━━━━━\n\n"
            "Digite o email da conta MASTER:\n"
            "(a que vai ser copiada)",
            parse_mode="Markdown"
        )
        return
    if text == "👥 Config Follower":
        user_states[uid] = {"action": "copy_follower"}
        await update.message.reply_text(
            "👥 *CONFIGURAR FOLLOWER*\n━━━━━━━━━━━━━━━━━━\n\n"
            "Digite o email da conta FOLLOWER:\n"
            "(a que vai copiar)",
            parse_mode="Markdown"
        )
        return
    if text == "▶️ Iniciar Copy":
        if uid in copytraders:
            await update.message.reply_text("Ja esta rodando!")
            return
        cfg = load_config().get(str(uid), {})
        master_email = cfg.get("copy_master")
        follower_email = cfg.get("copy_follower")
        if not master_email or not follower_email:
            await update.message.reply_text("Configure Master e Follower primeiro!")
            return
        master_uid = None
        follower_uid = None
        for uid_key, api in exnova_instances.items():
            try:
                email = getattr(api, '_login_email', '')
                if email == master_email:
                    master_uid = uid_key
                if email == follower_email:
                    follower_uid = uid_key
            except:
                pass
        if not master_uid or not follower_uid:
            await update.message.reply_text("Conecte as duas contas na EXNOVA primeiro!")
            return
        trader = CopyTrader(uid, context.bot, asyncio.get_running_loop(), master_uid, follower_uid)
        copytraders[uid] = trader
        threading.Thread(target=trader.run_loop, daemon=True).start()
        await update.message.reply_text("📋 Copytrader INICIADO!")
        return
    if text == "⏹ Parar Copy":
        if uid in copytraders:
            copytraders[uid].running = False
            del copytraders[uid]
            await update.message.reply_text("📋 Copytrader PARADO!")
        else:
            await update.message.reply_text("Nao esta rodando!")
        return
    if text == "📡 Sinais Ao Vivo":
        kb = [
            [KeyboardButton("▶️ Iniciar Monitor"), KeyboardButton("⏹ Parar Monitor")],
            [KeyboardButton("📋 Status Monitor")],
            [KeyboardButton("🔙 Voltar")],
        ]
        reply_markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
        await update.message.reply_text(
            "📡 *SINAIS AO VIVO*\n━━━━━━━━━━━━━━━━━━\n\n"
            "Como funciona:\n"
            "1. Entre no grupo de sinais\n"
            "2. *Encaminhe* o sinal pra mim\n"
            "3. Eu detecto e opero!\n\n"
            "Ou cole o sinal aqui:\n"
            "Ex: `Ativo: EURUSD-OTC`\n"
            "    `Direcao: COMPRA`\n"
            "    `Expiracao: M2`",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return
    if text == "➕ Adicionar Grupo":
        all_groups_file = os.path.join(DATA_DIR, "all_groups.json")
        try:
            with open(all_groups_file, 'r') as f:
                all_groups = json.load(f)
        except:
            all_groups = {}
        if all_groups:
            kb = []
            for gid, gname in all_groups.items():
                kb.append([KeyboardButton(f"➕ {gname} ({gid})")])
            kb.append([KeyboardButton("✏️ Digitar ID Manual")])
            kb.append([KeyboardButton("🔙 Voltar")])
            reply_markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
            await update.message.reply_text(
                "➕ *ADICIONAR GRUPO*\n━━━━━━━━━━━━━━━━━━\n\n"
                "Grupos onde o bot ja esta:\n"
                "Clique para adicionar:",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            user_states[uid] = {"action": "add_group_select"}
        else:
            user_states[uid] = {"action": "add_group"}
            await update.message.reply_text(
                "➕ *ADICIONAR GRUPO*\n━━━━━━━━━━━━━━━━━━\n\n"
                "O bot ainda nao esta em nenhum grupo!\n\n"
                "1. Adicione o bot ao grupo como *ADMIN*\n"
                "2. Digite o ID do grupo aqui\n\n"
                "Ex: `-1001234567890`",
                parse_mode="Markdown"
            )
        return
    if text == "✏️ Digitar ID Manual":
        user_states[uid] = {"action": "add_group"}
        await update.message.reply_text("Digite o ID do grupo (ex: -1001234567890):")
        return
    if text == "📋 Grupos Ativos":
        cfg = load_config().get(str(uid), {})
        groups = cfg.get("signal_groups", [])
        if groups:
            lista = "\n".join([f"• {g}" for g in groups])
            await update.message.reply_text(
                f"📋 *GRUPOS ATIVOS*\n━━━━━━━━━━━━━━━━━━\n\n{lista}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("Nenhum grupo adicionado!")
        return
    if text == "▶️ Iniciar Monitor":
        if uid in signal_monitors and signal_monitors[uid].running:
            await update.message.reply_text("Ja esta monitorando!")
            return
        cfg = load_config().get(str(uid), {})
        groups = cfg.get("signal_groups", [])
        if not groups:
            await update.message.reply_text("Adicione um grupo primeiro!")
            return
        if len(groups) == 1:
            monitor = SignalMonitor(uid, context.bot, asyncio.get_running_loop(), [groups[0]])
            signal_monitors[uid] = monitor
            threading.Thread(target=monitor.run_loop, daemon=True).start()
            await update.message.reply_text(
                f"📡 MONITOR INICIADO!\n"
                f"Monitorando: {groups[0]}"
            )
        else:
            kb = []
            for g in groups:
                kb.append([KeyboardButton(f"📡 {g}")])
            kb.append([KeyboardButton("🔙 Voltar")])
            reply_markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
            await update.message.reply_text(
                "📡 *ESCOLHA O GRUPO:*\n━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            user_states[uid] = {"action": "select_group"}
        return
    if text == "🗑 Excluir Grupo":
        cfg = load_config().get(str(uid), {})
        groups = cfg.get("signal_groups", [])
        if not groups:
            await update.message.reply_text("Nenhum grupo para excluir!")
            return
        kb = []
        for g in groups:
            kb.append([KeyboardButton(f"❌ {g}")])
        kb.append([KeyboardButton("🔙 Voltar")])
        reply_markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
        await update.message.reply_text(
            "🗑 *EXCLUIR GRUPO:*\n━━━━━━━━━━━━━━━━━━\n\nEscolha:",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        user_states[uid] = {"action": "delete_group"}
        return
    if text == "⏹ Parar Monitor":
        if uid in signal_monitors:
            signal_monitors[uid].running = False
            del signal_monitors[uid]
            await update.message.reply_text("📡 Monitor PARADO!")
        else:
            await update.message.reply_text("Nao esta monitorando!")
        return
    if text == "🎰 Gerenciamento":
        cfg = load_config().get(str(uid), {})
        kb = [
            [KeyboardButton("🔄 Martingale"), KeyboardButton("📈 Soros")],
            [KeyboardButton("🎰 Soros Gale"), KeyboardButton("🔁 Ciclos")],
            [KeyboardButton("💰 Valor Entrada")],
            [KeyboardButton("🔙 Voltar")],
        ]
        reply_markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
        msg = (
            f"🎰 *GERENCIAMENTO*\n━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Valor: R${cfg.get('amount', 1.0):.2f}\n"
            f"🔄 Martingale: {'✅ ON' if cfg.get('martingale', True) else '❌ OFF'}\n"
            f"   Mult: {cfg.get('mart_mult', 2.0)}x | Steps: {cfg.get('mart_steps', 3)}\n"
            f"📈 Soros: {'✅ ON' if cfg.get('soros', True) else '❌ OFF'}\n"
            f"   Mult: {cfg.get('soros_mult', 1.5)}x | Steps: {cfg.get('soros_steps', 3)}\n"
            f"🔁 Ciclos: {cfg.get('cycles', 0) or '∞'}\n"
            f"\nEscolha uma opcao:"
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)
        return
    if text == "🔄 Martingale":
        cfg = load_config().get(str(uid), {})
        user_states[uid] = {"action": "mart_mult"}
        await update.message.reply_text(
            f"🔄 *MARTINGALE*\n━━━━━━━━━━━━━━━━━━\n\n"
            f"Atual: {'✅ ON' if cfg.get('martingale', True) else '❌ OFF'}\n"
            f"Multiplicador: {cfg.get('mart_mult', 2.0)}x\n"
            f"Max steps: {cfg.get('mart_steps', 3)}\n\n"
            f"Digite o *multiplicador*:\n"
            f"Ex: `2` (dobra apos LOSS)\n"
            f"Ex: `2.5` (2.5x apos LOSS)",
            parse_mode="Markdown"
        )
        return
    if text == "📈 Soros":
        cfg = load_config().get(str(uid), {})
        user_states[uid] = {"action": "soros_mult"}
        await update.message.reply_text(
            f"📈 *SOROS*\n━━━━━━━━━━━━━━━━━━\n\n"
            f"Atual: {'✅ ON' if cfg.get('soros', True) else '❌ OFF'}\n"
            f"Multiplicador: {cfg.get('soros_mult', 1.5)}x\n"
            f"Max steps: {cfg.get('soros_steps', 3)}\n\n"
            f"Digite o *multiplicador*:\n"
            f"Ex: `1.5` (aumenta 50% apos WIN)\n"
            f"Ex: `2` (dobra apos WIN)",
            parse_mode="Markdown"
        )
        return
    if text == "🎰 Soros Gale":
        cfg = load_config().get(str(uid), {})
        user_states[uid] = {"action": "soros_steps"}
        await update.message.reply_text(
            f"🎰 *SOROS GALE*\n━━━━━━━━━━━━━━━━━━\n\n"
            f"Atual: {cfg.get('soros_steps', 3)} steps\n\n"
            f"Quantos steps maximos?\n"
            f"Ex: `3` (ate 3 gales)\n"
            f"Ex: `5` (ate 5 gales)",
            parse_mode="Markdown"
        )
        return
    if text == "🔁 Ciclos":
        user_states[uid] = {"action": "cycles"}
        await update.message.reply_text(
            "🔁 *CONFIGURAR CICLOS*\n━━━━━━━━━━━━━━━━━━\n\n"
            "Digite os valores de cada ciclo:\n"
            "Formato: `valor1, valor2`\n\n"
            "Exemplo:\n"
            "`2.5, 5.7`\n"
            "`13.0, 29.0`\n"
            "`65.0, 145.0`\n\n"
            "Cada linha = 1 ciclo",
            parse_mode="Markdown"
        )
        return
    if text == "💰 Valor Entrada":
        user_states[uid] = {"action": "entry_amount"}
        await update.message.reply_text("Valor da entrada (ex: 5.0):")
        return
    if text == "🔙 Voltar":
        await cmd_auto_trade(update, context)
        return
    if text == "📋 Lista Sinais":
        user_states[uid] = {"action": "signal_list"}
        await update.message.reply_text(
            "📋 *LISTA DE SINAIS*\n━━━━━━━━━━━━━━━━━━\n\n"
            "Cole a lista no formato:\n"
            "`TIMEFRAME;ATIVO;HORA DIRECAO`\n\n"
            "Exemplo:\n"
            "`M1;EURGBP-OTC;21:11 CALL`\n"
            "`M5;GBPUSD-OTC;21:15 PUT`\n"
            "`M1;BTCUSD-OTC;22:00 CALL`\n\n"
            "Separe cada linha com Enter",
            parse_mode="Markdown"
        )
        return
    if text == "🔄 Mudar conta":
        api = get_exnova(uid)
        if api:
            mode = api.get_balance_mode()
            new_mode = "REAL" if mode == "PRACTICE" else "PRACTICE"
            api.change_balance(new_mode)
            await update.message.reply_text(f"Mudado para: {new_mode}")
        return
    if text == "📋 Ativos":
        api = get_exnova(uid)
        if api:
            api.update_ACTIVES_OPCODE()
            ativos = api.get_all_ACTIVES_OPCODE()
            lista = "\n".join([f"• {k}" for k in list(ativos.keys())[:20]])
            await update.message.reply_text(f"📋 *ATIVOS*\n{lista}", parse_mode="Markdown")
        return
    if text == "🔙 Menu":
        await start(update, context)
        return

    if not state:
        await update.message.reply_text("Use /start")
        return

    action = state.get("action")

    if action == "exnova_email":
        user_states[uid] = {"action": "exnova_pass", "email": text}
        await update.message.reply_text("Senha:")
        return
    if action == "exnova_pass":
        email = state["email"]
        password = text
        await update.message.reply_text("Conectando...")
        loop = asyncio.get_running_loop()
        def do_connect():
            ok, msg = connect_exnova(uid, email, password)
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(f"{'Conectado!' if ok else f'Erro: {msg}'}"),
                loop
            )
        threading.Thread(target=do_connect, daemon=True).start()
        user_states[uid] = {}
        return

    if action == "cfg_active":
        user_states[uid] = {"action": "cfg_amount", "active": text.upper()}
        await update.message.reply_text(f"Ativo: {text.upper()}\nValor ($):")
        return
    if action == "cfg_amount":
        try:
            amount = float(text)
        except:
            await update.message.reply_text("Numero!")
            return
        user_states[uid] = {"action": "cfg_expiration", "active": state["active"], "amount": amount}
        await update.message.reply_text(f"Valor: ${amount}\nExpiracao (min):")
        return
    if action == "cfg_expiration":
        try:
            expiration = int(text)
        except:
            await update.message.reply_text("Numero!")
            return
        cfg = load_config()
        if str(uid) not in cfg:
            cfg[str(uid)] = {}
        cfg[str(uid)]["active"] = state["active"]
        cfg[str(uid)]["amount"] = state["amount"]
        cfg[str(uid)]["expiration"] = expiration
        save_config(cfg)
        user_states[uid] = {}
        await update.message.reply_text(f"CONFIG SALVA!\nAtivo: {state['active']}\nValor: ${state['amount']}\nExpiracao: {expiration}min")
        return

    if action == "mart_mult":
        try:
            mult = float(text)
        except:
            await update.message.reply_text("Numero!")
            return
        user_states[uid] = {"action": "mart_steps", "mart_mult": mult}
        await update.message.reply_text(
            f"Multiplicador: {mult}x\n\n"
            f"Agora digite o *max steps*:\n"
            f"Ex: `3` (ate 3 gales)\n"
            f"Ex: `5` (ate 5 gales)",
            parse_mode="Markdown"
        )
        return

    if action == "mart_steps":
        try:
            steps = int(text)
        except:
            await update.message.reply_text("Numero!")
            return
        cfg = load_config()
        if str(uid) not in cfg:
            cfg[str(uid)] = {}
        cfg[str(uid)]["mart_mult"] = state["mart_mult"]
        cfg[str(uid)]["mart_steps"] = steps
        cfg[str(uid)]["martingale"] = True
        save_config(cfg)
        user_states[uid] = {}
        await update.message.reply_text(
            f"✅ MARTINGALE SALVO!\n\n"
            f"Multiplicador: {state['mart_mult']}x\n"
            f"Max steps: {steps}"
        )
        return

    if action == "soros_mult":
        try:
            mult = float(text)
        except:
            await update.message.reply_text("Numero!")
            return
        user_states[uid] = {"action": "soros_steps", "soros_mult": mult}
        await update.message.reply_text(
            f"Multiplicador: {mult}x\n\n"
            f"Agora digite o *max steps*:\n"
            f"Ex: `3` (ate 3 gales)\n"
            f"Ex: `5` (ate 5 gales)",
            parse_mode="Markdown"
        )
        return

    if action == "soros_steps":
        try:
            steps = int(text)
        except:
            await update.message.reply_text("Numero!")
            return
        cfg = load_config()
        if str(uid) not in cfg:
            cfg[str(uid)] = {}
        cfg[str(uid)]["soros_mult"] = state["soros_mult"]
        cfg[str(uid)]["soros_steps"] = steps
        cfg[str(uid)]["soros"] = True
        save_config(cfg)
        user_states[uid] = {}
        await update.message.reply_text(
            f"✅ SOROS SALVO!\n\n"
            f"Multiplicador: {state['soros_mult']}x\n"
            f"Max steps: {steps}"
        )
        return

    if action == "cycles":
        try:
            cycles = int(text)
        except:
            await update.message.reply_text("Numero!")
            return
        cfg = load_config()
        if str(uid) not in cfg:
            cfg[str(uid)] = {}
        cfg[str(uid)]["cycles"] = cycles
        save_config(cfg)
        user_states[uid] = {}
        msg = f"Ciclos: {'∞' if cycles == 0 else cycles}"
        await update.message.reply_text(msg)
        return

    if action == "entry_amount":
        try:
            amount = float(text)
        except:
            await update.message.reply_text("Numero!")
            return
        cfg = load_config()
        if str(uid) not in cfg:
            cfg[str(uid)] = {}
        cfg[str(uid)]["amount"] = amount
        save_config(cfg)
        user_states[uid] = {}
        await update.message.reply_text(f"✅ Valor entrada: R${amount:.2f}")
        return

    if action == "copy_master":
        email = text.strip()
        cfg = load_config()
        if str(uid) not in cfg:
            cfg[str(uid)] = {}
        cfg[str(uid)]["copy_master"] = email
        save_config(cfg)
        user_states[uid] = {}
        await update.message.reply_text(f"✅ Master configurado: {email}")
        return

    if action == "copy_follower":
        email = text.strip()
        cfg = load_config()
        if str(uid) not in cfg:
            cfg[str(uid)] = {}
        cfg[str(uid)]["copy_follower"] = email
        save_config(cfg)
        user_states[uid] = {}
        await update.message.reply_text(f"✅ Follower configurado: {email}")
        return

    if action == "add_group":
        group_id = text.strip()
        cfg = load_config()
        if str(uid) not in cfg:
            cfg[str(uid)] = {}
        groups = cfg[str(uid)].get("signal_groups", [])
        if group_id not in groups:
            groups.append(group_id)
            cfg[str(uid)]["signal_groups"] = groups
            save_config(cfg)
            await update.message.reply_text(f"✅ Grupo adicionado: {group_id}")
        else:
            await update.message.reply_text("Grupo ja existe!")
        user_states[uid] = {}
        return

    if action == "select_group":
        group = text.replace("📡 ", "").strip()
        monitor = SignalMonitor(uid, context.bot, asyncio.get_running_loop(), [group])
        signal_monitors[uid] = monitor
        threading.Thread(target=monitor.run_loop, daemon=True).start()
        await update.message.reply_text(f"📡 MONITOR INICIADO!\nMonitorando: {group}")
        user_states[uid] = {}
        return

    if action == "delete_group":
        group = text.replace("❌ ", "").strip()
        cfg = load_config()
        if str(uid) in cfg:
            groups = cfg[str(uid)].get("signal_groups", [])
            if group in groups:
                groups.remove(group)
                cfg[str(uid)]["signal_groups"] = groups
                save_config(cfg)
                await update.message.reply_text(f"✅ Grupo removido: {group}")
            else:
                await update.message.reply_text("Grupo nao encontrado!")
        user_states[uid] = {}
        return

    if action == "signal_list":
        lines = text.strip().split('\n')
        signals = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split(';')
            if len(parts) >= 3:
                timeframe = parts[0].strip().upper()
                active = parts[1].strip().upper()
                rest = parts[2].strip().split()
                hora = rest[0].strip()
                direcao = rest[1].strip().upper() if len(rest) > 1 else "CALL"
                signals.append({
                    'timeframe': timeframe,
                    'active': active,
                    'time': hora,
                    'direction': direcao
                })

        if not signals:
            await update.message.reply_text("Formato invalido! Use:\n`TIMEFRAME;ATIVO;HORA DIRECAO`", parse_mode="Markdown")
            return

        signal_file = os.path.join(DATA_DIR, f"signals_{uid}.json")
        with open(signal_file, 'w') as f:
            json.dump(signals, f, indent=2)

        lista = "\n".join([f"• {s['timeframe']} | {s['active']} | {s['time']} {s['direction']}" for s in signals[:20]])
        total = len(signals)

        await update.message.reply_text(
            f"📋 *{total} SINAIS SALVOS!*\n━━━━━━━━━━━━━━━━━━\n\n{lista}\n\n"
            f"{'...' if total > 20 else ''}\nUse ▶️ Iniciar Auto pra operar",
            parse_mode="Markdown"
        )
        user_states[uid] = {}
        return

    if action == "admin_license":
        try:
            parts = text.split()
            set_license(int(parts[0]), int(parts[1]))
            await update.message.reply_text("Licenca salva!")
        except:
            await update.message.reply_text("Use: ID DIAS")
        user_states[uid] = {}
        return
    if action == "admin_addadmin":
        try:
            admins_set.add(int(text))
            await update.message.reply_text("Admin adicionado!")
        except:
            await update.message.reply_text("ID invalido!")
        user_states[uid] = {}
        return

    await update.message.reply_text("Use /start")

async def cmd_ativos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    api = get_exnova(uid)
    if not api:
        await update.message.reply_text("❌ Conecte na EXNOVA primeiro!")
        return
    try:
        from exnovaapi import OP_code
        actives = sorted([k for k in OP_code.ACTIVES.keys() if 'OTC' in k])
        lista = "\n".join([f"• {a}" for a in actives[:50]])
        await update.message.reply_text(
            f"📋 Ativos OTC disponiveis na EXNOVA:\n━━━━━━━━━━━━━━━━━━\n{lista}\n\n"
            f"Total: {len(actives)} ativos OTC\n"
            f"Total geral: {len(OP_code.ACTIVES)}",
            parse_mode=None
        )
    except Exception as e:
        await update.message.reply_text(f"Erro ao listar ativos: {e}")


def main():
    print("INICIANDO LEVERAGE INVEST Bot...")
    if not TOKEN:
        print("TOKEN nao encontrado!")
        return
    app = Application.builder().token(TOKEN).request(
        HTTPXRequest(connect_timeout=60.0, read_timeout=60.0)
    ).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("conectar", cmd_conectar))
    app.add_handler(CommandHandler("operar", cmd_operar))
    app.add_handler(CommandHandler("auto_trade", cmd_auto_trade))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("admin_license", cmd_admin_license))
    app.add_handler(CommandHandler("admin_addadmin", cmd_admin_addadmin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("ativos", cmd_ativos))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot rodando!")
    app.run_polling()

if __name__ == "__main__":
    main()
