# Crypto AI Agent — Análise Técnica em Tempo Real

Agente de IA para análise técnica de criptomoedas nos perpétuos da Binance.
Interface responsiva para PC e celular.

---

## Funcionalidades

- **Indicadores**: RSI, MACD, Bollinger Bands, EMA 9/21/50/200, ATR, ADX, Stochastic RSI, OBV, Supertrend
- **Padrões detectados no gráfico**: LTA, LTB, Canal (alta/baixa/horizontal), Triângulos, Cunhas, Topo/Fundo Duplo, OCO/OCO Invertido, Bandeiras
- **Sinais**: Entrada, Stop Loss, TP1/TP2/TP3, Risco/Retorno
- **Tipo de operação**: Scalp, Day Trade, Swing Trade, HODL
- **Análise IA** com Claude (Anthropic)
- **Multi-timeframe**: 1m, 5m, 15m, 1h, 4h, 1d
- **Preço ao vivo** via WebSocket
- **Lista completa** de perpétuos USDT da Binance

---

## Requisitos

- Python 3.10+
- Node.js 18+

---

## Instalação

### 1. Backend

```bash
cd backend

# Criar ambiente virtual
python -m venv .venv
source .venv/bin/activate          # Linux/Mac
# .venv\Scripts\activate           # Windows

# Instalar dependências
pip install -r requirements.txt

# Configurar variáveis de ambiente
cp .env.example .env
# Edite o .env com suas chaves da Binance e Anthropic
```

### 2. Frontend

```bash
cd frontend
npm install
```

---

## Configuração

Edite `backend/.env`:

```env
BINANCE_API_KEY=sua_chave_aqui         # Opcional — leitura pública não requer chave
BINANCE_SECRET_KEY=sua_secret_aqui     # Opcional
ANTHROPIC_API_KEY=sua_chave_claude     # Para análise IA (claude.ai/settings/api-keys)
```

> **Nota**: A Binance permite leitura de dados de mercado sem autenticação.
> As chaves da Binance são necessárias apenas para operações de conta (ordens, saldo, etc.).
> A chave Anthropic é necessária para a análise em linguagem natural — sem ela, uma análise
> textual local é gerada automaticamente.

---

## Executar

### Iniciar o backend

```bash
cd backend
source .venv/bin/activate
python main.py
# Servidor em http://localhost:8000
```

### Iniciar o frontend

```bash
cd frontend
npm run dev
# App em http://localhost:3000
```

Abra **http://localhost:3000** no navegador.

---

## API Endpoints

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| GET | `/api/symbols` | Lista todos os perpétuos USDT |
| GET | `/api/ohlcv?symbol=BTC/USDT:USDT&timeframe=1h` | Dados OHLCV |
| GET | `/api/analyze?symbol=BTC/USDT:USDT&timeframe=1h` | Análise completa |
| GET | `/api/multi-timeframe?symbol=BTC/USDT:USDT` | Análise em múltiplos TFs |
| GET | `/api/market-data?symbol=BTC/USDT:USDT` | Ticker, funding rate, OI |
| GET | `/api/watchlist/analyze?symbols=BTC/USDT:USDT,ETH/USDT:USDT&timeframe=1h` | Análise rápida de lista |
| WS | `/ws/price/{symbol}` | Preço ao vivo |
| WS | `/ws/analysis/{symbol}?timeframe=1h` | Análise ao vivo (30s) |

---

## Estrutura do Projeto

```
├── backend/
│   ├── main.py                    # FastAPI app
│   ├── config.py
│   ├── requirements.txt
│   ├── .env.example
│   ├── models/
│   │   └── trade_signal.py        # Modelos Pydantic
│   └── services/
│       ├── binance_service.py     # Dados Binance (ccxt)
│       ├── indicator_service.py   # Indicadores técnicos (pandas-ta)
│       ├── pattern_service.py     # Detecção de padrões
│       ├── signal_service.py      # Geração de sinais
│       └── ai_service.py          # Análise Claude AI
└── frontend/
    ├── src/
    │   ├── App.tsx                # App principal (PC + Mobile)
    │   ├── components/
    │   │   ├── Chart/
    │   │   │   └── CandleChart.tsx  # Gráfico TradingView + padrões
    │   │   └── SignalPanel/
    │   │       └── SignalPanel.tsx  # Painel de sinais
    │   ├── hooks/
    │   │   ├── useAnalysis.ts
    │   │   ├── useSymbols.ts
    │   │   └── useLivePrice.ts
    │   ├── services/
    │   │   └── api.ts
    │   └── types/
    │       └── index.ts
    └── package.json
```

---

## Padrões Detectados

| Padrão | Tipo | Direção |
|--------|------|---------|
| LTA (Linha de Tendência de Alta) | Tendência | Bullish |
| LTB (Linha de Tendência de Baixa) | Tendência | Bearish |
| Canal Ascendente | Canal | Bullish |
| Canal Descendente | Canal | Bearish |
| Canal Horizontal | Canal | Neutro |
| Triângulo Simétrico | Triangle | Neutro |
| Triângulo Ascendente | Triangle | Bullish |
| Triângulo Descendente | Triangle | Bearish |
| Cunha Ascendente | Wedge | Bearish |
| Cunha Descendente | Wedge | Bullish |
| Topo Duplo | Reversão | Bearish |
| Fundo Duplo | Reversão | Bullish |
| OCO (Ombro-Cabeça-Ombro) | Reversão | Bearish |
| OCO Invertido | Reversão | Bullish |
| Bandeira de Alta | Continuação | Bullish |
| Bandeira de Baixa | Continuação | Bearish |
