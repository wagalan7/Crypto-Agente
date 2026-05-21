/**
 * Crypto-Agente Binance Proxy — Cloudflare Worker
 *
 * Encaminha requisições pra Binance Futures (fapi.binance.com) usando o IP
 * do Cloudflare, que a Binance aceita (vs. Railway que recebe 451 geo-block).
 *
 * Endpoints suportados (whitelist — só leitura, zero risco de trading):
 *   /fapi/v1/klines          → OHLCV
 *   /fapi/v1/ticker/24hr     → ticker 24h
 *   /fapi/v1/premiumIndex    → funding rate
 *   /fapi/v1/openInterest    → open interest
 *   /fapi/v1/exchangeInfo    → símbolos disponíveis
 *
 * Uso pelo backend:
 *   GET https://crypto-proxy.SEU_USER.workers.dev/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=300
 *
 * Free tier: 100.000 requests/dia. Nosso scan usa ~1.500/dia → sobra demais.
 */

const ALLOWED_PATHS = new Set([
  "/fapi/v1/klines",
  "/fapi/v1/ticker/24hr",
  "/fapi/v1/ticker/price",
  "/fapi/v1/premiumIndex",
  "/fapi/v1/openInterest",
  "/fapi/v1/exchangeInfo",
  "/fapi/v1/depth",
  "/fapi/v1/aggTrades",
  // Spot fallback (mesmo proxy serve os dois)
  "/api/v3/klines",
  "/api/v3/ticker/24hr",
  "/api/v3/exchangeInfo",
]);

const TARGETS = {
  fapi: "https://fapi.binance.com",
  spot: "https://api.binance.com",
};

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400",
};

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    if (request.method !== "GET") {
      return jsonError(405, "Method not allowed");
    }

    if (!ALLOWED_PATHS.has(url.pathname)) {
      return jsonError(403, `Path not allowed: ${url.pathname}`);
    }

    // Escolhe target: /fapi/* vai pra futures, /api/* vai pra spot
    const targetBase = url.pathname.startsWith("/fapi/")
      ? TARGETS.fapi
      : TARGETS.spot;

    const upstream = `${targetBase}${url.pathname}${url.search}`;

    try {
      const resp = await fetch(upstream, {
        method: "GET",
        headers: {
          "User-Agent": "Mozilla/5.0 (compatible; CryptoAgentProxy/1.0)",
          Accept: "application/json",
        },
        cf: {
          // Cache curto (60s) pra rate-limit-friendly em chamadas repetidas
          cacheTtl: 60,
          cacheEverything: true,
        },
      });

      const body = await resp.text();
      return new Response(body, {
        status: resp.status,
        headers: {
          ...CORS_HEADERS,
          "Content-Type": resp.headers.get("Content-Type") || "application/json",
          "X-Proxy-Upstream": targetBase,
          "X-Proxy-Status": String(resp.status),
        },
      });
    } catch (err) {
      return jsonError(502, `Upstream fetch failed: ${err.message}`);
    }
  },
};

function jsonError(status, message) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}
