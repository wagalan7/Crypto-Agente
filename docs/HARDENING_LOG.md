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
