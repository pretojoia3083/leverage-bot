# LEVERAGE INVEST — Bot Telegram (Exnova)

Bot de Telegram para operações em opções binárias via API da [Exnova](https://exnova.com)
(wrapper [`exnovaapi`](https://github.com/CassDs/exnovaapi)).

## O que ele faz

O bot opera em **três modos**, todos rodando dentro do próprio processo Telegram:

| Modo | Classe | Descrição |
|---|---|---|
| **Auto Trade** | `AutoTrader` | Opera sozinho usando indicador próprio (3 velas + pullback + rompimento + pavios) |
| **Copiar Sinais** | `SignalCopier` | Lê sinais de grupos do Telegram e replica a operação |
| **Master/Follower** | `MasterFollower` | Copia as entradas de uma conta mestre para uma conta seguidora |

Recursos embutidos: **Martingale**, **Soros Gale**, expiração configurável, lista de sinais
pré-carregados, licenças por usuário e painel de admin.

## Estrutura

```
BOTTELEGRAN/
├── bot_backtest.py     ← BOT PRINCIPAL (versão oficial)
├── database.py         ← camada MongoDB (licenças, admins, configs)
├── logo.png
├── requirements.txt
├── .env                ← NÃO versionado (ver abaixo)
├── data/               ← config e dados locais (NÃO versionado)
│   ├── bot_config.json
│   ├── contas_salvas.json
│   └── signals_<user_id>.json
├── scripts/
│   └── iniciar_bot.bat ← atalho para rodar no Windows
└── _legacy/            ← versões antigas (bot.py, bot_forex.py, etc.)
```

## Configuração (.env)

Crie um arquivo `.env` na raiz com:

```env
TELEGRAM_TOKEN=123456789:SEU_TOKEN_AQUI
ADMIN_ID=SEU_TELEGRAM_ID
MONGO_URI=mongodb+srv://...
```

## Instalar e rodar

```bash
pip install -r requirements.txt
```

No Windows, dê duplo-clique em `scripts/iniciar_bot.bat`.
Ou manualmente, **a partir da raiz**:

```bash
python bot_backtest.py
```

## Comandos do bot

**Usuário:** `/start` · `/status` · `/conectar` · `/operar` · `/auto_trade` · `/ativos` · `/cancel`
**Admin:** `/admin` · `/admin_license <id> <dias>` · `/admin_addadmin <id>`

---

## ⚠️ Avisos importantes

1. **API não oficial.** A Exnova **não publica API de negociação**. A biblioteca `exnovaapi`
   é engenharia reversa de terceiros sobre endpoints privados. Ela pode parar de funcionar
   sem aviso e o uso pode violar os termos de serviço da corretora (risco de bloqueio de conta).
2. **Risco financeiro.** Opções binárias são produtos de altíssimo risco; a maioria dos
   operadores amadores perde dinheiro. Martingale/Soros multiplicam perdas rapidamente.
3. Este software é uma ferramenta técnica. **Não é recomendação de investimento.**
