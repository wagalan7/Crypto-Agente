# Hardening Log

## P01 - Baseline e testes de caracterização

- Status: Concluído
- Início real: 19/08/2026
- Branch: `codex/hardening-p01`
- Base: `feat/decouple-exec-universe` em `58531c1f`

### Mapa do fluxo crítico

| Responsabilidade | Arquivo principal | Observação de baseline |
|---|---|---|
| Geração, score e tier | `backend/services/recommendation_service.py` | V1/V2, gates de RR/volume/MTF e CT downgrade |
| Tendência e regimes | `backend/services/regime_service.py` | CT brake falha aberto e exige tendência HTF inequívoca |
| Sizing e orquestração | `backend/services/shadow_trade_service.py` | Cap de margem, teto duro de risco e mínimo de notional |
| Entrada e proteção | `backend/services/binance_signed_service.py` | Entrada e SL/TP são sequenciais; dedup existe nas proteções |
| Gestão pós-entrada | `backend/services/trade_manager_service.py` | TP1, BE estrutural, time-stop, auto-heal e runner |
| Resultado de snapshots | `backend/services/snapshot_service.py` | Usa R fixo por status para pesquisa e agregação histórica |
| Risco por snapshots | `backend/services/risk_service.py` | DD soma `realized_r * risk_pct` dos snapshots |
| Kill switch live | `backend/services/kill_switch_service.py` | Deriva limites do histórico de `RealTrade` |
| Calibração | `backend/services/calibration_service.py` | Shrinkage e isotônica sobre outcomes resolvidos |
| Rotação | `backend/services/rotation_service.py` | Promoção/demissão com histerese e seed de backtest |
| Backtest | `backend/services/backtest_service.py` e `recommendation_backtest.py` | Caminho separado do scan live; equivalência ainda não demonstrada |

### Caracterização adicionada

- sizing: cap duro de risco, cap de margem e rejeição do mínimo de notional;
- proteção: contrato SL + TP1 + TP2 e dedup de SL vivo;
- CT brake: contra tendência inequívoca e fail-open em MTF misto;
- snapshot: mapeamento fixo de status real para R de pesquisa;
- MTF sintético: somente timeframes estritamente superiores.

### Validação

- Comando: `python -m unittest backend.tests.test_hardening_characterization -v`
- Resultado: **9 testes aprovados**.
- Banco e exchange: não acessados; dependências externas foram simuladas.

### Lacunas observadas para os próximos pacotes

- Não há suíte pytest/unittest do backend de trading; existia apenas script manual de circuit breaker.
- Proteções são criadas depois da entrada e o contrato atual não fecha emergencialmente a posição se o SL falhar.
- Dedup de proteção é fail-open quando a leitura da exchange é incerta.
- `risk_service` ainda usa R teórico de snapshots; o `kill_switch_service` usa `RealTrade`, criando duas fontes de risco.
- A documentação chama campos `ema9/ema21`, mas o cálculo histórico usa 12/26.
- O backtest e o live não têm teste de paridade de decisão/execução.

### Decisão

P01 somente caracteriza o baseline. Nenhuma regra de negócio, ENV ou caminho live foi alterado.

## P02 - Stop fail-safe e rollback da entrada

- Status: Concluído localmente (não publicado/não ativado em conta real)
- Início real: 19/08/2026
- Dependência: P01 concluído em `8b655fda`

### Implementação

- Estado pós-fill explícito: entrada, proteção e safety state retornados ao caller.
- SL só é confirmado quando a Binance retorna `algoId` não vazio.
- Falha do SL interrompe a criação de TP1/TP2.
- Exceção durante a proteção também aciona rollback.
- Fechamento emergencial MARKET usa lado oposto, `reduceOnly=true` e a quantidade realmente preenchida.
- Fill parcial fecha apenas o saldo restante em novas tentativas.
- Sucesso exige `positionRisk` fresco e zerado; cache stale/rate-limited nunca comprova fechamento.
- Timeout-com-sucesso é reconciliado antes de repetir a ordem.
- Toda MARKET recebe `clientOrderId`; submissão ambígua é consultada pelo ID e,
  enquanto continuar UNKNOWN, recebe somente tentativa de SL total, sem novo
  MARKET ou TP.
- Condicionais conhecidas e ordens com o mesmo prefixo são removidas somente após FLAT confirmado.
- Falha não confirmada pausa novas entradas em `RiskState`, dispara alertas e mantém um `RealTrade` aberto para visibilidade/auto-heal.
- Latch local mantém o processo em quarentena mesmo se a persistência da pausa falhar.
- Maker parcial cancela no primeiro fill observado, preserva o lower-bound de qty
  e só dimensiona proteção/rollback quando a qty terminal é comprovada.
- Leitura stale/cache nunca fecha trade no DB; flip, time-stop, expiração e poeira
  só encerram após posição fresh-flat e cleanup confirmado.
- Gestão live, backfill e trades managed ficam restritos à Binance neste pacote;
  mismatch/typo de exchange falha fechado.
- Incidentes sem SL aparecem como `SEM SL CONFIRMADO` na interface e ignoram a
  carência normal do auto-heal.

### Configurações novas

- `BINANCE_EMERGENCY_CLOSE_ATTEMPTS` (default `3`).
- `BINANCE_EMERGENCY_CLOSE_RETRY_DELAY` (default `0.35` segundo, backoff linear).
- `BINANCE_POST_FILL_PROTECTION_TIMEOUT_S` (default `8` segundos por fase: SL e TPs).
- `BINANCE_MAKER_CANCEL_CONFIRM_ATTEMPTS` (default `2`).
- `BINANCE_MAKER_CANCEL_CONFIRM_DELAY` (default `0.25` segundo).

Defaults fail-closed mantidos no P02:

- `MAKER_ENTRY_ENABLED=false` até reconciliação persistente do P03;
- `TF_UPGRADE_ENABLED=false`;
- `PYRAMIDING_ENABLED=false`.

### Validação

- **56 testes aprovados** cobrindo proteção normal, `algoId` ausente, SL
  rejeitado, exceção, timeout-com-sucesso, fill parcial, maker parcial, qty
  terminal desconhecida, stale position, cleanup, quarentena e cross-exchange.
- `py_compile` aprovado para o backend alterado, incluindo `backend/main.py`.
- Build de produção da interface aprovado (`tsc && vite build`).
- `git diff --check` aprovado.
- Banco e exchange reais não foram acessados; integrações foram simuladas.

### Riscos restantes fora do P02

- Reconciliação persistente/assíncrona de entry maker UNKNOWN e de conditionals
  que materializem muito depois do timeout: P03. O maker permanece OFF por default.
- Revalidação de preço e política de fallback MARKET maker: P04.
- Boot reconcile ainda deve transformar posição `untracked` em pausa persistente
  após restart; até o P03, o latch cobre o processo atual e o relatório acusa drift.
- Conta Binance em hedge mode não está suportada explicitamente; o fluxo presume one-way mode.
- Bybit live permanece bloqueada até receber o mesmo contrato transacional.
