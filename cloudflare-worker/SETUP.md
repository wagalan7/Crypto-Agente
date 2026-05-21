# Setup Cloudflare Worker — Binance Proxy

Esse Worker dá um IP "amigo" pra Binance, permitindo o backend Railway acessar
`fapi.binance.com` sem geo-block (451).

## Passo a passo (15 min, conta grátis)

### 1. Criar conta Cloudflare

Vai em https://dash.cloudflare.com/sign-up e cria conta grátis. Não precisa
verificar cartão nem configurar domínio.

### 2. Criar o Worker

1. No painel CF, menu lateral: **Workers & Pages**
2. Clica **Create application** → **Create Worker**
3. Nome sugerido: `crypto-proxy` (pode ser outro, mas anota)
4. Clica **Deploy** (vai criar um Worker "hello world" padrão)

### 3. Colar o código

1. Depois do deploy inicial, clica **Edit code**
2. Apaga tudo que tem no editor
3. Cola o conteúdo de `worker.js` (deste diretório) inteiro
4. Clica **Deploy** (botão azul, topo direita)

### 4. Pegar a URL

Depois do deploy, o CF mostra a URL pública. Algo tipo:
```
https://crypto-proxy.SEU_USERNAME.workers.dev
```

**Copia essa URL.**

### 5. Testar (opcional mas recomendado)

Abre no navegador:
```
https://crypto-proxy.SEU_USERNAME.workers.dev/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=2
```

Se retornar um JSON com candles do BTC → funcionou ✅

### 6. Adicionar no Railway

No serviço **Crypto-Agente** (Railway), adiciona a variável:

```
BINANCE_PROXY_URL=https://crypto-proxy.SEU_USERNAME.workers.dev
```

Railway re-deploya sozinho. O backend vai detectar a variável e passar a
usar o Worker pro server-scan → dados Binance Futures iguais ao app aberto.

## Quota e custos

- **Free tier**: 100.000 requests/dia
- **Nosso uso**: ~1.500/dia (scan 5min × 40 símbolos × 3 TFs ÷ cache 60s)
- **Sobra**: 98.500/dia. Nunca vamos cobrar.

## Troubleshooting

- **403 "Path not allowed"**: Worker tá funcionando mas tentou endpoint fora
  da whitelist. Avisa pra eu adicionar.
- **502 Upstream fetch failed**: Binance derrubou a conexão (raro). Worker
  já retry no próximo ciclo de scan.
- **451 ainda**: significa que o Worker tá rodando num data center que a
  Binance também bloqueia (raríssimo). Solução: ativar Cloudflare Argo ou
  mudar a região default na dashboard. Me avisa.

## Segurança

- Worker só aceita métodos GET
- Whitelist explícita de paths (zero endpoints de trading)
- Sem autenticação custom (dados são públicos da Binance)
- Sem armazenamento de estado no Worker
- CORS aberto (já que backend Railway é o único cliente útil)
