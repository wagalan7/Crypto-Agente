# Lote final local — o que uma publicação futura precisaria

Nada deste documento foi executado. Não houve push, deploy, alteração de
ambiente, ordem, mensagem, acesso externo ou ativação. Ele existe para que a
revisão e a publicação aconteçam depois sem descobrir dependência escondida.

## 1. Alteração operacional intencional (única)

**Bloco A — intenção de entrada idempotente (`SAFETY_FIX`).** É a única mudança
que altera comportamento com os defaults atuais: cada decisão reserva e commita
uma intenção econômica ANTES da primeira mutação de ordem, e o
`client_order_id` passa a derivar dela.

- Efeito pretendido: a mesma decisão observada em snapshots distintos, um
  restart no meio do envio ou duas conexões concorrentes produzem UM envio.
- Efeito colateral aceito: quando o despacho fica sem confirmação, a intenção
  vira `UNKNOWN` e o símbolo espera a reconciliação em vez de reenviar. Isso
  pode ADIAR uma entrada — por contrato, nunca duplicá-la.
- Prova local: 20 testes herméticos e 14 cenários em PostgreSQL real
  (concorrência, crash antes/depois do envio, lease vencido, callback tardio,
  conflito de payload, capacidade) com zero chamada de exchange.

Todo o resto do lote nasce desligado e não muda nada sem ação explícita.

## 2. Migrações

Todas **aditivas e idempotentes** (`CREATE TABLE IF NOT EXISTS` /
`ADD COLUMN IF NOT EXISTS`), aplicadas pelo `init_db` e exercitadas no
PostgreSQL descartável. Nenhuma coluna existente foi alterada ou removida.

| O quê | Onde | Bloco |
| --- | --- | --- |
| tabela `entry_intents` | nova | A (P03) |
| coluna `entry_intents.dispatch_ids` (JSONB) | aditiva | A (P03) |
| tabela `policy_simulation_state` | nova | C (R11) |
| coluna `policy_simulation_state.payload` (JSONB) | aditiva | C (R11) |
| coluna `entry_intents.decision_payload` (JSONB) | aditiva | A (P03) |

- A evidência pré-seleção **não** cria tabela: reutiliza
  `decision_observations`, `rejected_setup_observations` e
  `decision_observation_attempts`, separando a coorte pelo campo `scope`.
- O experimento pré-seleção **não** cria tabela: reutiliza
  `strategy_experiments` gravando o TIPO versionado dentro de
  `candidate_config` (envelope `experiment_type`/`experiment_type_version`).
- Retenção, índices e histórico existentes não foram alterados.

## 3. Configurações NOVAS (todas com default inativo)

| Variável | Default | Ligando o quê |
| --- | --- | --- |
| `R05_FINANCIAL_TOTAL_SOURCE` | `legacy` | total com funding (R05D) |
| `R11_POLICY_VERSION` | `legacy` | política robusta (R11C) |
| `R07_STRATEGY_CORE_MODE` | `inactive` | núcleo de estratégia em simulação |
| `R08_SCORE_V3_MODE` | `inactive` | Score V3 de pesquisa |
| `R09_PRESELECTION_MODE` | `inactive` | coleta de evidência pré-seleção |
| `R10_PORTFOLIO_REPLAY_MODE` | `inactive` | replay de carteira |
| `R10_WALK_FORWARD_MODE` | `inactive` | validação walk-forward |
| `R12_PRE_SELECTION_MODE` | `inactive` | experimento pré-seleção |

Valor desconhecido — e explicitamente `live` — resolve para inativo. Ativação é
sempre separada da publicação do código: publicar não liga nada.

## 4. Ordem das publicações futuras

**Publicação 1 — código inativo + coleta.** Sobe o lote com todos os seletores
no default, confirma que o resumo `lote_final` mostra apenas o bloco A ativo e,
só depois, liga `R09_PRESELECTION_MODE=observe` para acumular evidência
pré-seleção. Pré-condições: nenhum incidente P03 aberto, orçamento de coleta
(50 registros por lote, 200 em buffer, 20 mil oportunidades) confirmado em
produção e cobertura acompanhada como métrica, não como resultado.

**Publicação 2 — canário aprovado.** Só existe se a evidência da publicação 1
cumprir o gate congelado (100 trades shadow, 30 por playbook, 14 dias, 10 dias
úteis, 90% de cobertura, EV, incerteza, drawdown, estabilidade, zero duplicata
econômica, nenhuma falha de proteção aberta) E houver autorização humana. O
manifest de canário descreve versão, diff, evidência, critérios, pré-condições,
responsáveis e rollback — e não aplica nada por si.

**Eventual publicação 3 — dependência de dado.** A liquidez pré-seleção depende
de profundidade de book NO INSTANTE da decisão, que o acervo atual não guarda
(`ORDERBOOK_DEPTH_AT_DECISION`, declarada inativa). Se essa dimensão for
exigida pelo gate, ela vira uma publicação própria de coleta, ANTES do canário —
sem inventar histórico.

## 5. Rollback

- Desligar qualquer seletor restaura o comportamento legado imediatamente; não
  há estado novo a limpar nos blocos B–H.
- O ensaio local de rollback preserva posições abertas, proteções, intenções,
  ledgers, histórico e incidentes, e recusa DDL destrutivo, restauração de dado
  inválido e regressão de correção de segurança.
- O bloco A não tem rollback por flag: reverter exigiria reverter o commit, e
  isso reintroduziria o risco de entrada dupla. Se for preciso, a reversão
  precisa de decisão humana explícita e reconciliação das intenções abertas.

## 6. Limites de desempenho medidos localmente

- Registro do funil pré-seleção: 500 registros em menos de 2 s; payload
  congelado abaixo de 4 KiB por decisão.
- Coleta pré-seleção: no máximo 50 registros por lote e 200 em buffer; teto de
  admissão de 20 mil oportunidades e 40 mil tentativas, sem apagar histórico.
- Exportador: limite de 16 MiB por artefato e paginação mantidos.
- Replay de carteira: limitado por `max_bars` do R10A; nenhuma consulta nova.

## 7. Coleta e validação externas ainda necessárias

1. Amostra prospectiva pré-seleção em produção (nada foi coletado).
2. Validação econômica com custos observados de conta — hoje só há cenário
   declarado; `observed_account_costs` é sempre `false`.
3. Calibração própria da V3 com amostra fora da amostra; sem ela não há
   probabilidade, tier nem aprovação econômica.
4. Profundidade de book no instante da decisão, se a liquidez pré-seleção
   entrar no gate.
5. Aprovação humana e canário — nenhum dos dois foi solicitado ou preparado
   para aplicação.

## 8. Fechamento das integrações — o que muda numa publicação futura

Migrações ADITIVAS novas (criadas no boot por `init_db`, sem passo manual):

- `entry_intents.dispatch_ids JSONB` — ids efetivos de despacho gravados antes
  do envio (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`).
- tabela `policy_simulation_state` — histerese/geração da simulação R11/R12.

Nenhuma variável de ambiente NOVA. Os seletores continuam os mesmos e com o
mesmo default inativo; `R09_PRESELECTION_MODE=observe` agora realmente coleta,
então ligá-lo é uma decisão operacional com efeito (linhas `PRE_SELECTION` no
acervo existente, dentro do orçamento já declarado).

Mudanças de comportamento com os defaults atuais (todas do caminho P03, que é
`SAFETY_FIX` e já estava ativo):

1. a admissão passa a respeitar o teto de posições configurado e o risco aberto
   lido sob a lock — entradas que antes passavam por um limite desligado podem
   ser recusadas com `MAX_OPEN_POSITIONS`/`MAX_OPEN_RISK`;
2. risco aberto ilegível bloqueia nova entrada (`OPEN_RISK_UNKNOWN`);
3. sem credencial comprovada não há identidade de decisão e nada é enviado;
4. cada POST de entrada exige o guard da intenção e o id efetivo registrado.

Rollback: desligar os seletores restaura o legado dos blocos B–H; o caminho P03
não tem flag — reverter exigiria reverter os commits e reconciliar as intenções
abertas, com decisão humana explícita.

## 9. Correção integrada (25/09/2026) — o que muda numa publicação

### Segurança ATIVA por default × pesquisa INATIVA por default

- **Ativo sempre** (não tem seletor): P03 (intenção/reconciliação) e o gate
  financeiro R05B quando o cutover já estiver ligado. São correções de
  segurança: só podem ADIAR ou RECUSAR uma entrada, nunca criar uma.
- **Inativo por default** (só liga por variável de ambiente): R05D, R07/R08,
  R09, R10 e R11/R12. Nenhum deles decide entrada com o default atual; o
  entrypoint de pesquisa é local e declara `LIVE_ADAPTER_NOT_IMPLEMENTED`.
- Nenhuma variável de ambiente NOVA nesta correção.

### Impactos esperados de P03 (segurança, já ativa)

1. Intenção com desfecho PROVADO agora ENCERRA no mesmo ciclo: o incidente não
   reabre pela mesma prova e o slot/risco voltam — antes a intenção ficava
   `UNKNOWN` para sempre, segurando capacidade e quarentena.
2. Fill comprovado e protegido vincula o RealTrade (`CONFIRMED`) e a reserva
   vira exposição. Sem prova de QUALQUER id despachado (inclusive a filha
   `-mfb`), nada encerra — continua bloqueando, como antes.

### Impactos esperados de R05 (limite diário)

1. O pior cenário do dia passa a incluir as reservas de OUTRAS intenções
   pendentes. Com o cutover ligado, entradas que antes passavam por pouco podem
   ser recusadas com `FINANCIAL_WORST_CASE_LIMIT` — o caso de referência é
   P&L −92, reserva alheia 6 e proposta 3 contra limite 100: −95 passava, −101
   bloqueia.
2. Reserva ilegível, conta não identificada ou banco indisponível ⇒
   `RESERVATIONS_UNAVAILABLE`/`DAILY_BUDGET_UNKNOWN` e a entrada é bloqueada.
   Desconhecido não vira zero.
3. Duas decisões concorrentes não consomem mais a mesma margem: a admissão e o
   orçamento são decididos sob a MESMA lock transacional.
4. O risco FINAL (preço/qty revalidados antes do POST) é readmitido; se ele não
   couber, a entrada é recusada mesmo com a reserva menor já concedida.

Nada disso aumenta limite, alavancagem ou exposição, e nenhuma trava foi
afrouxada para o teste passar.

## 10. Fechamento dos oito achados (28/09/2026)

Baseline `46c7d7f0`. Cada achado foi reproduzido (RED) antes da correção.

### Migração NOVA

`entry_intents.decision_payload JSONB` (aditiva, idempotente, criada pelo
`init_db`): entry/stop/qty da decisão gravados ANTES do primeiro POST. Sem eles
a reconciliação não consegue nem ADOTAR um stop já existente. Linha antiga fica
sem o campo — e continua fail-closed, sem estimativa.

### Impactos de SEGURANÇA (P03/R05, ativos por default)

1. **Fim sem rastro deixou de existir.** Uma entrada preenchida e já encerrada
   não vira mais `RECONCILED_NO_EXECUTION`: exige RealTrade correspondente
   (símbolo, exchange, lado e alvo único). Sem vínculo, o incidente vai a
   MANUAL_REQUIRED, a pausa P03 é rearmada e o slot continua ocupado — mais
   pendências humanas explícitas do que antes, por desenho.
2. **Recuperação com dados.** Incidentes da recuperação passam a nascer com
   `planned_stop`/`planned_qty`, então um SL vivo e válido é ADOTADO em vez de
   ficar em RETRY_PENDING eterno.
3. **Orçamento diário consistente.** A base do dia é recalculada DENTRO da
   transação da admissão. Uma reserva que virou posição entre a leitura e a
   lock agora conta: entradas que antes passavam por pouco podem ser recusadas
   com `DAILY_LOSS_LIMIT`. Base ilegível bloqueia (`DAILY_BASE_UNAVAILABLE`).
   Continua valendo só com o cutover R05B ligado — a correção NÃO o liga.

### Impactos de PESQUISA (inativa por default)

4. **Cronologia.** O motor publica `result_available_ts_ms`: dentro da vela não
   há sequência observável e TIME_STOP/MAX_HOLD consomem o `close`, então o
   resultado só é utilizável quando a vela FECHA. Capital, slot e exposição
   seguem essa disponibilidade — números de replays anteriores mudam.
5. **Walk-forward.** O treino de cada dobra só consome label disponível no
   corte; sem instante confiável, a dobra roda o fallback baseline. As etapas
   publicadas passam a dizer o que REALMENTE rodou.
6. **Comparação.** O contraste executado é MANAGEMENT_ONLY e o relatório diz o
   que ele NÃO prova. A hipótese autorizada (baseline × candidata congeladas)
   está `BLOCKED_MISSING_DECISION`: falta decisão registrada em contrato.
7. **Simulação R11.** Publicação por CAS de geração; cálculo obsoleto é
   recusado e recalculado.
8. **Catálogo R12.** O tipo pré-seleção ganhou criação, avaliação e transições
   próprias na tabela oficial, terminando em SHADOW de simulação. Promoção
   declarada `PRE_SELECTION_PROMOTION_NOT_IMPLEMENTED`.

Nenhum default foi ligado, nenhuma estratégia nova foi criada e o adaptador
LIVE continua ausente.

## 11. Fechamento A + C + H (28/09/2026)

Sem migração nova neste pacote: A usa o `payload` JSONB que o incidente já tem
e H reutiliza `policy_simulation_state`.

Impactos na ADMISSÃO (segurança, ativa por default):

1. **A — mais pendências explícitas.** Um incidente resolvido por cleanup (ou
   legado) sem consulta terminal da entry NÃO encerra mais a intenção: o id
   volta para a fila, o reconciliador consulta a identidade exata e, até lá,
   slot, reserva e quarentena continuam ocupados. Encerrar como "não executou"
   agora exige zero comprovado em TODOS os dispatches.
2. **C — bloqueio correto em corrida.** As parcelas do dia vêm de uma única
   leitura pós-lock. Uma posição que fecha entre leituras deixa de sumir da
   conta, então entradas que antes passavam por pouco podem ser recusadas com
   `DAILY_LOSS_LIMIT`; leitura indisponível recusa com
   `ADMISSION_SNAPSHOT_UNAVAILABLE`. Continua valendo só com o cutover R05B
   ligado — este pacote NÃO o liga.

Impacto na PESQUISA (inativa por default):

3. **H — aprovação só com estudo vinculado.** Criar/avaliar um experimento
   pré-seleção exige o estudo persistido com identidade conferida; números
   soltos não conferem OFFLINE_VALIDATED e a avaliação oficial nunca lê a
   coorte pós-seleção. Promoção continua declarada como não implementada.
