# Lote final local — contratos, versões e invariantes

Baseline `e3878238`. Tudo aqui é LOCAL: nada foi publicado, ativado ou aprovado.
Somente o bloco A (`SAFETY_FIX`) muda comportamento por padrão; todo o resto
nasce inativo atrás de um seletor.

## 1. Mapa dos módulos

| Bloco | Módulo | Versão | Seletor (default) | Contrato |
| --- | --- | --- | --- | --- |
| A | `services/entry_intent_service.py`, `models/entry_intent.py` | `p03.intent.v1` | — (ativo) | `SAFETY_FIX` |
| B | `services/financial_total_service.py` | `R05D_TOTAL_WITH_FUNDING_V1` | `R05_FINANCIAL_TOTAL_SOURCE=legacy` | `CANDIDATE_POLICY` |
| C | `services/robust_policy_service.py` | `R11C_ROBUST_V1` | `R11_POLICY_VERSION=legacy` | `CANDIDATE_POLICY` |
| D | `services/strategy_core_service.py` | `R07D_STRATEGY_CORE_V1` | `R07_STRATEGY_CORE_MODE=inactive` | `CANDIDATE_POLICY` |
| D | `services/score_v3_service.py` | `R08D_SCORE_V3_RESEARCH_V1` | `R08_SCORE_V3_MODE=inactive` | `CANDIDATE_POLICY` |
| E | `services/preselection_observation_service.py` | `r09.pre.v1` | `R09_PRESELECTION_MODE=inactive` | `OBSERVATION_ONLY` |
| F | `services/research_dataset_scopes.py` | escopos R10B | `scope` opcional (default legado) | `OBSERVATION_ONLY` |
| F | `services/portfolio_replay_service.py` | `R10D_PORTFOLIO_REPLAY_V1` | `R10_PORTFOLIO_REPLAY_MODE=inactive` | `CANDIDATE_POLICY` |
| F | `services/walk_forward_service.py` | `R10E_WALK_FORWARD_V1` | `R10_WALK_FORWARD_MODE=inactive` | `CANDIDATE_POLICY` |
| G | `services/preselection_experiment_service.py` | `R12_PRE_SELECTION_EXPERIMENT_V1` | `R12_PRE_SELECTION_MODE=inactive` | `CANDIDATE_POLICY` |
| H | `services/research_batch_service.py` (`lote_final`) | `schema_version=1` | — (somente leitura) | `OBSERVATION_ONLY` |

Seletor desconhecido — inclusive `live` — resolve para o modo inativo/legado.
Nenhum módulo novo é importado por `main.py`, pelo executor ou pelo scanner.

## 2. Invariantes que os testes cobram

**Entrada econômica (A).** Uma decisão = uma intenção persistida antes da
primeira mutação de ordem; `client_order_id` deriva da intenção; estados
`RESERVED → SENDING → CONFIRMED | UNKNOWN | TERMINAL` com compare-and-set,
unicidade no banco e lease por dono. Lease vencido após despacho vira `UNKNOWN`
e NUNCA reenvia.

**Total financeiro (B).** `pnl_usd` continua líquido EX-funding; o total novo só
AGREGA o que o ledger R05C já provou. `COMPLETE` exige todas as linhas
confirmadas, funding confirmado, taxas completas, nenhum ativo não convertido,
conta e ativo de liquidação coerentes e coleta provada. Insuficiência bloqueia
AUMENTO de exposição e nunca proteção, redução ou fechamento.

**Política robusta (C).** Amostra deduplicada por identidade da oportunidade,
populações separadas, janelas de decay disjuntas, histerese que exige período E
evidência nova, geração atômica do multiplicador aprendido e limiar com correção
de múltiplas comparações.

**Núcleo de estratégia (D).** Estado ponto-no-tempo validado (vela fechada,
série contígua, relógio coerente, evidência HTF não futura), config explícita
com hash e procedência por parâmetro, decisão sempre com motivos, features
ausentes, playbook/versão e trace. Três playbooks espelhados long/short. Stop
estrutural: R:R abaixo do piso REPROVA o setup — o stop não é alargado, o TP não
é inventado. Conflito HTF confirmado BLOQUEIA continuação e também a reversão de
range; força HTF desconhecida é tratada como conflito. Arbitragem determinística
sem soma de votos. `opportunity_key` casa com a identidade do P03.

**Score V3 (D).** Allowlist com unidade, domínio, sentido, peso, cap e chave de
evidência ÚNICA; seis categorias com cap somando 100. ADX é força e muda de
sentido conforme o playbook, sem nunca definir lado. Funding é direcional em
parcela única. O composto de confluência fica fora por padrão e, quando ligado,
SUBSTITUI estrutura e gatilho com normalização contínua. Ausente sai da escala;
inválido não vira evidência; cobertura abaixo do piso indisponibiliza o score.
Sem calibração V3 do mesmo fingerprint não há probabilidade, tier, aprovação
econômica nem elegibilidade LIVE — e bins/p_global da V2 são recusados.

**Evidência pré-seleção (E).** Escopo `PRE_SELECTION` aditivo sobre o acervo
R09; `POST_SELECTION` intocado. Etapa não avaliada nunca vira aprovada; a
primeira rejeição é fato, não causa contrafactual. Aceitas e vetadas
compartilham identidade. Horizonte por timeframe e pelo maior horizonte entre os
candidatos registrados. Origem divergente vira `SOURCE_MISMATCH`; ausente vira
`UNLABELED`. Falha e completude são cobertura, com `pnl_assumption=None`.

**Replay e validação (F).** A trajetória continua no motor R10A. Em volta:
universo ponto-no-tempo, latência, preço executável com gate revalidado, maker
sem fill / parcial / fallback apenas quando habilitados, carteira compartilhada
(capital, reservas, exposição, slots, um por símbolo) e custos separados por
componente. Walk-forward exige ≥3 dobras que avançam e não se sobrepõem, purga
pelo maior horizonte, embargo, ajuste só no treino e teste final que não escolhe.
Custo, horizonte, cobertura ou intervalo insuficientes ⇒ sem vencedor.

**Experimento e gate (G).** Tipo versionado dentro do `StrategyExperiment`
existente; contratos pré e pós-seleção não são intercambiáveis; bloqueios
legados preservados. Um challenger no ciclo oficial, lifecycle sem saltos,
congelamento de baseline/candidato/config/custos/proteções. Gate congelado: 100
trades shadow, 30 por playbook, 14 dias, 10 dias úteis, 90% de cobertura, EV,
incerteza, drawdown, estabilidade, zero duplicata econômica e nenhuma falha de
proteção aberta. Passar no gate mantém `live_approval=UNAVAILABLE`.

## 3. Regras congeladas antes de qualquer outcome

- `CoreConfig` e `ScoreConfig` têm hash e manifest com `outcomes_consulted=false`,
  `optimized=false` e `approved_for_production=false`.
- Procedência por parâmetro: `production_equivalent` (com a constante de origem)
  ou `engineering_choice` (com a justificativa). Nenhum parâmetro foi escolhido
  por resultado; não houve grid search nem tuning.
- `GoNoGoCriteria` tem `criteria_hash`; mudar qualquer mínimo muda o hash.
- Fingerprint do Score V3 inclui fórmula, features, pesos, caps, playbook e
  população — calibração de outro fingerprint não serve.

## 4. Matriz de fidelidade (replay de carteira)

| Dimensão | Status | Observação |
| --- | --- | --- |
| `decision_rule` | `PROVEN` | a mesma regra pura decide em simulação e replay |
| `point_in_time_universe` | `PROVEN` com snapshots; senão `UNAVAILABLE` | universo atual não substitui o histórico |
| `price_path` | `MODELED` | OHLCV não prova caminho intrabar |
| `entry_fill` | `MODELED` com cotação; senão `UNAVAILABLE` | cotação sintética, não book histórico |
| `queue_position` | `UNAVAILABLE` | OHLCV não prova fila |
| `partial_fills` | `UNAVAILABLE` por padrão | só `MODELED` quando explicitamente habilitado |
| `latency` | `MODELED` | latência declarada, não medida em produção |
| `fees` / `funding` / `slippage_liquidity` | `MODELED` com custo completo; senão `UNAVAILABLE` | custo ausente nunca vira zero |
| `portfolio_limits` | `MODELED` | limites simulados |
| `exchange_rejections` | `UNAVAILABLE` | rejeição de exchange não é reproduzida |

`live_equivalent` é `false` em todos os artefatos. Cenário não é execução
observada, e compartilhar função não transforma um no outro.

## 5. Paridade com o legado

- Exportador R10B: escopo legado gera SQL byte a byte igual e o mesmo
  `request_hash` quando nenhum escopo é informado.
- Laboratório V2 do R08A permanece intocado: mesmos pesos e mesmo resultado
  antes e depois de rodar a V3.
- `POST_SELECTION`, `P051_ANALYTICS_ONLY`, champion 12/26, V1/V2 e a calibração
  75–100 seguem como estavam.
- Com todos os seletores ausentes, o resumo do lote mostra o bloco A como único
  ativo — e ele é a correção de segurança.
