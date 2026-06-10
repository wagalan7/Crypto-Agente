# Crypto Win — memória do projeto

Idioma: **PT-BR** em toda comunicação. Stack: FastAPI async + React/Vite/TS +
PostgreSQL (async SQLAlchemy/asyncpg) + Binance. **Go-live mainnet: sexta 12/06.**
Regra dura: **não quebrar código que funciona.**

## Os dois ambientes (NÃO CONFUNDIR)

| Apelido oficial | Papel | Serviço Railway | URL |
|---|---|---|---|
| **PRD** | Produção. O bot opera de verdade. Sexta vai a dinheiro real. | `Crypto-Agente` (produção) | `https://crypto-agente-production.up.railway.app` |
| **Crypto-Agente-Dev** | Ambiente de **TESTES** (champion/challenger). Shadow, DB próprio, push off. Universo amplo. O bot **NÃO** opera com dinheiro. | `Crypto-Agente-DEV` (projeto Railway: `Crypto-Agente-Dev`) | `https://crypto-agente-production-c6c4.up.railway.app` |

- Sempre que eu disser "ambiente de testes" / "Dev" = **Crypto-Agente-Dev** (`-c6c4`).
- PRD e Dev têm **DBs separados** de propósito (o universo amplo do Dev não pode
  contaminar o auto-learner do PRD).

## Regra de execução (segurança go-live)

- **PRD opera SÓ as 60** (top-60 por volume). Isso é firme pra sexta.
- **NÃO subir `SERVER_SCAN_TOP_N` do PRD** pra mostrar mais moedas: as mesmas
  `recs` alimentam display + snapshots + **execução** (`open_shadow_for_recs`),
  e o `portfolio_guard` (slots: 5 posições / 2 por categoria / 5% risco) daria
  slot pra moeda de score alto fora das 60 → **mudaria o que o bot opera**.
- Universo amplo (observação/aprendizado) vem do **Crypto-Agente-Dev**, não do PRD.
- `SERVER_SCAN_TOP_N` é lido **só no boot** (backend/services/recommendation_service.py:100)
  → mudar a env exige **redeploy/restart** do serviço.

## App — painel "Trades Recomendados"

- **🤖 BOT OPERA** = recs do PRD (as 60 que o bot executa).
- **👁 OBSERVAÇÃO** = recs do Crypto-Agente-Dev (universo amplo, só pra analisar).
- Fonte da observação: `VITE_OBSERVATION_API_URL` (default = URL do `-c6c4`).
  Degrada gracioso: se o Dev cair, mostra só as do bot.

## Operacional

- Railway CLI **deslogado** → validação só via `curl` HTTP. Não consigo mexer em
  env do Railway nem ver logs; passos no dashboard são do usuário.
- Deploy = `git push origin main` (Railway auto-deploy). Confirmar conclusão pelo
  `startup_at` em `/api/health`. Preflight de gates: `/api/live/preflight`.
- Promoção Dev→PRD (futuro): ≥20 trades · expectancy>0 · win-rate≥alvo · ~2 semanas.
