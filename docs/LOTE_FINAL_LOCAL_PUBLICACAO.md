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

- Bloco A: tabela `entry_intents` (aditiva). Já criada e exercitada no
  PostgreSQL descartável; nenhuma coluna existente foi alterada.
- Blocos B–H: **nenhuma migração**. A evidência pré-seleção reutiliza
  `decision_observations`, `rejected_setup_observations` e
  `decision_observation_attempts`; o experimento pré-seleção reutiliza
  `strategy_experiments` gravando o tipo dentro de `candidate_config`.
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
