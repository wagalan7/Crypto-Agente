# Projeto de Hardening e Evolução do Bot de Cripto

## 1. Objetivo

Tornar o bot seguro para um canário real de baixo risco e, depois, melhorar assertividade, expectativa líquida e quantidade de oportunidades sem simplesmente afrouxar filtros.

O projeto não promete lucratividade. Cada mudança só avança após testes automatizados, shadow trading e métricas objetivas.

## 2. Estratégia para economizar tokens no Claude Code

Usar **12 prompts de execução**, um por pacote atômico. Não enviar novamente toda a especificação do app em cada conversa.

Antes do Prompt 1, salvar no repositório:

- este arquivo;
- `PROMPT_COMPLETO_APP.md` em `docs/ESPECIFICACAO_ATUAL.md`;
- resultados de testes e decisões em `docs/HARDENING_LOG.md`.

Em cada nova execução, pedir ao Claude Code para ler apenas:

1. este projeto;
2. a seção indicada da especificação;
3. os arquivos diretamente envolvidos;
4. o último registro relevante do log.

Regras de economia:

- um prompt = um objetivo e um commit;
- limitar a busca aos diretórios citados;
- não pedir explicações longas nem reprodução de arquivos inteiros;
- exigir primeiro inspeção curta e depois implementação;
- executar testes focados antes da suíte completa;
- registrar decisões no log para não redescobrir contexto;
- abrir nova sessão quando mudar de pacote;
- não misturar backend, frontend e pesquisa estatística no mesmo prompt.

## 3. Definição de pronto global

O canário real só pode ser considerado elegível quando:

- nenhuma posição puder permanecer sem stop confirmado;
- entradas duplicadas forem impedidas por idempotência transacional;
- fallback MARKET revalidar preço, slippage, stop e R:R;
- candles e dados contextuais tiverem controle de validade;
- circuit breakers usarem P&L líquido/exchange equity, não R fixo teórico;
- EMA e nomenclaturas de score estiverem corrigidas;
- estratégias de tendência e reversão estiverem separadas;
- backtest incluir taxas, funding, slippage, atraso e ambiguidade intrabar;
- métricas shadow cobrirem no mínimo 100 trades e 30 por playbook habilitado;
- não houver regressões na suíte automatizada;
- rollback e kill switch tiverem sido ensaiados.

## 4. Fases, ordem e cronograma

Premissas: início em **10/08/2026**, uma pessoa usando Claude Code, cinco dias úteis por semana. Datas são metas iniciais e devem ser atualizadas no acompanhamento.

| ID | Pacote | Dependência | Duração | Início | Fim planejado | Status |
|---|---|---|---:|---|---|---|
| P01 | Baseline, mapa real e testes de caracterização | — | 2 dias | 10/08 | 11/08 | Não iniciado |
| P02 | Stop fail-safe e rollback da entrada | P01 | 2 dias | 12/08 | 13/08 | Não iniciado |
| P03 | Idempotência e proteção contra entradas duplicadas | P01 | 2 dias | 14/08 | 17/08 | Não iniciado |
| P04 | Staleness e revalidação do fallback MARKET | P02, P03 | 2 dias | 18/08 | 19/08 | Não iniciado |
| P05 | P&L líquido e circuit breakers reais | P01 | 3 dias | 20/08 | 24/08 | Não iniciado |
| P06 | Correção de EMA e semântica de scores/probabilidades | P01 | 2 dias | 25/08 | 26/08 | Não iniciado |
| P07 | Playbooks separados por regime | P06 | 4 dias | 27/08 | 01/09 | Não iniciado |
| P08 | Score V3 e política contra tendência | P07 | 3 dias | 02/09 | 04/09 | Não iniciado |
| P09 | Funil de rejeições e shadow dos sinais vetados | P07 | 3 dias | 08/09 | 10/09 | Não iniciado |
| P10 | Backtest realista e validação walk-forward | P05, P08 | 5 dias | 11/09 | 17/09 | Não iniciado |
| P11 | Learning, edge decay e rotação robustos | P09, P10 | 3 dias | 18/09 | 22/09 | Não iniciado |
| P12 | Shadow, critérios go/no-go e canário real | Todos | 10 dias úteis mínimos | 23/09 | 06/10 | Não iniciado |

Feriado nacional de 07/09 foi excluído. O tempo de shadow é mínimo e deve ser ampliado se não atingir a amostra necessária.

### Marcos

| Marco | Condição | Data-alvo |
|---|---|---|
| M1 — Execução protegida | P02–P05 aprovados | 24/08 |
| M2 — Sinal reestruturado | P06–P08 aprovados | 04/09 |
| M3 — Medição confiável | P09–P11 aprovados | 22/09 |
| M4 — Decisão de canário | Shadow e checklist P12 aprovados | 06/10 ou depois |

## 5. Controle de andamento

Atualizar esta tabela ao final de cada prompt:

| ID | Status | Início real | Fim real | Atraso | Commit/PR | Bloqueio | Próxima ação |
|---|---|---|---|---:|---|---|---|
| P01 | Não iniciado | — | — | 0 | — | — | Executar Prompt 1 |
| P02 | Não iniciado | — | — | 0 | — | — | Aguardar P01 |
| P03 | Não iniciado | — | — | 0 | — | — | Aguardar P01 |
| P04 | Não iniciado | — | — | 0 | — | — | Aguardar P02/P03 |
| P05 | Não iniciado | — | — | 0 | — | — | Aguardar P01 |
| P06 | Não iniciado | — | — | 0 | — | — | Aguardar P01 |
| P07 | Não iniciado | — | — | 0 | — | — | Aguardar P06 |
| P08 | Não iniciado | — | — | 0 | — | — | Aguardar P07 |
| P09 | Não iniciado | — | — | 0 | — | — | Aguardar P07 |
| P10 | Não iniciado | — | — | 0 | — | — | Aguardar P05/P08 |
| P11 | Não iniciado | — | — | 0 | — | — | Aguardar P09/P10 |
| P12 | Não iniciado | — | — | 0 | — | — | Aguardar todos |

Status permitidos: `Não iniciado`, `Em andamento`, `Bloqueado`, `Em validação`, `Concluído`.

## 6. Cabeçalho padrão dos prompts

Este cabeçalho já está incorporado nos prompts abaixo. Se o repositório real estiver em outra branch, trocar para ela antes do Prompt 1.

```text
Trabalhe somente no pacote indicado. Leia docs/PROJETO_HARDENING_BOT.md e as seções/arquivos citados. Preserve alterações alheias. Não altere parâmetros sem teste ou justificativa mensurável. Primeiro inspecione o fluxo real e escreva em até 8 linhas o plano; depois implemente. Adicione testes focados, execute-os e corrija falhas. Não faça refatorações fora do escopo. Ao terminar, atualize docs/HARDENING_LOG.md com arquivos alterados, testes, riscos restantes e decisão. Responda apenas com resumo, testes e pendências; não reproduza arquivos inteiros.
```

## 7. Prompts prontos para Claude Code

### Prompt 1 — Baseline e testes de caracterização

```text
Trabalhe somente no P01 de docs/PROJETO_HARDENING_BOT.md. Leia a especificação atual e localize no código os fluxos reais de sinal, execução, proteção, P&L, breakers, backtest e rotação. Confirme divergências entre documento e código. Crie docs/HARDENING_LOG.md e um mapa curto arquivo→responsabilidade. Adicione apenas testes de caracterização para os comportamentos críticos existentes, sem mudar regra de negócio. Cubra no mínimo sizing/caps, criação de SL/TP, cálculo de realized R/P&L, tier/CT brake e deduplicação. Execute testes focados. Atualize o status P01 e registre lacunas. Não faça refatoração funcional.
```

Aceite: mapa do código real, baseline reproduzível e testes que travem o comportamento atual.

### Prompt 2 — Stop fail-safe

```text
Trabalhe somente no P02. Inspecione binance_signed_service e o fluxo que abre a posição e cria proteções. Implemente uma máquina de estado explícita: entrada confirmada → SL confirmado → TPs confirmados. Se o SL não for confirmado após tentativas curtas e limitadas, feche imediatamente a posição com ordem reduce-only apropriada e registre falha crítica. TP ausente pode ir ao auto-heal; SL ausente não pode aguardar o grace normal. Garanta tratamento de fill parcial, timeout e resposta ambígua da exchange. Não mude a estratégia de sinal. Adicione testes para sucesso, rejeição do SL, timeout, fill parcial e falha no fechamento emergencial. Atualize log/status.
```

Aceite: nenhuma saída do fluxo deixa posição conhecida aberta sem stop ou fechamento emergencial acionado.

### Prompt 3 — Idempotência de entrada

```text
Trabalhe somente no P03. Mapeie todos os caminhos de entrada manual, auto e shadow. Implemente chave idempotente persistente por decisão operacional (símbolo, lado, timeframe/setup e janela/gatilho), protegida por constraint/transação no banco. Requisições repetidas, scans concorrentes, restart e timeout de resposta da Binance não podem abrir uma segunda posição. Reconcilie por clientOrderId antes de reenviar ordem. Preserve upgrades/flips legítimos com regras explícitas. Adicione testes concorrentes e de retry/restart. Atualize log/status e migration necessária.
```

Aceite: chamadas concorrentes e retries produzem no máximo uma entrada econômica.

### Prompt 4 — Dados válidos e fallback MARKET

```text
Trabalhe somente no P04. Adicione validação central de frescor para candle, ticker, funding, OI, regime e macro, com limites configuráveis e fail-closed nos dados essenciais. Antes do fallback MARKET, busque preço atual e recalcule distância da entrada, slippage, stop efetivo, R:R líquido, anti-chase, notional e qty. Cancele se ultrapassar max_valid_entry, slippage, spread ou RR mínimo. Não altere critérios de geração de sinal. Adicione reason codes estruturados e testes de dados stale, preço favorável, chase, spread alto e RR degradado. Atualize log/status.
```

Aceite: MARKET nunca é usado com setup vencido ou risco diferente do validado.

### Prompt 5 — P&L e circuit breakers reais

```text
Trabalhe somente no P05. Faça o risk_service usar fills e P&L líquido realizado da exchange/RealTrade, incluindo parcial, taxas, funding, slippage, fechamento manual e runner. Mantenha realized_r de snapshots apenas para pesquisa, nunca como fonte financeira primária do kill switch. Defina fonte de verdade, reconciliação e comportamento fail-closed quando houver divergência. Garanta que risco aberto + uma perda plausível não ultrapassem silenciosamente o limite diário. Adicione testes de TP1+BE, TP1+stop estrutural, TP2, parcial, funding/taxas e reconciliação divergente. Atualize log/status.
```

Aceite: breakers e dashboard batem com P&L líquido dos fills dentro da tolerância definida.

### Prompt 6 — EMA e semântica dos scores

```text
Trabalhe somente no P06. Corrija de ponta a ponta a divergência ema9/ema21 que calcula EMA12/EMA26. Escolha uma convenção única baseada no uso real e faça migration/compatibilidade se dados persistidos ou API pública forem afetados. Renomeie conceitos semânticos: qualidade geométrica de padrão não é probabilidade; score bruto não é P(win). Somente valores produzidos pela calibração podem usar nomes calibrated_probability. Preserve compatibilidade de API quando necessário com campos deprecated. Adicione testes dos períodos, serialização e consumidores. Atualize documentação, log e status.
```

Aceite: período, nome, UI e lógica MTF dizem a mesma coisa; nenhuma heurística é apresentada como probabilidade calibrada.

### Prompt 7 — Playbooks por regime

```text
Trabalhe somente no P07. Substitua a votação única que mistura tendência e reversão por playbooks independentes, mantendo a versão antiga atrás de feature flag. Implemente inicialmente TREND_PULLBACK, TREND_BREAKOUT e RANGE_REVERSION. Cada playbook deve declarar regimes válidos, features permitidas, gatilho, invalidação, stop, alvo e reason codes. RSI/Bollinger/Stochastic não podem votar contra tendência dentro de playbook de continuação; MACD/EMA/SuperTrend não devem forçar continuação em range. Exija candle fechado. Não ajuste pesos para maximizar backtest nesta tarefa. Adicione testes de cenários sintéticos bull, bear, range e conflito. Atualize log/status.
```

Aceite: cada recomendação identifica um playbook e nenhuma soma mistura premissas incompatíveis.

### Prompt 8 — Score V3 e contra tendência

```text
Trabalhe somente no P08. Crie SCORE_FORMULA_V3 atrás de feature flag. ADX deve medir força, nunca direção. Evite contar a mesma evidência de preço várias vezes: agrupe features correlacionadas por categoria e aplique caps. Inclua regime/MTF, estrutura, gatilho, qualidade da entrada, volume/liquidez, derivativos e EV líquido calibrado, com decomposição auditável. Enquanto não houver playbook de reversão validado, sinais contra HTF devem ser bloqueados, não apenas rebaixados. Não remova V1/V2. Adicione testes de monotonicidade, dados ausentes, conflito MTF e decomposição do score. Atualize log/status.
```

Aceite: score explicável, ADX não muda direção e operação contra HTF não escapa por downgrade.

### Prompt 9 — Funil de rejeição

```text
Trabalhe somente no P09. Instrumente o pipeline com reason codes estáveis e um funil por etapa: candidato, playbook, candle, RR, volume, MTF/regime, risco e execução. Grave em shadow o resultado hipotético dos sinais rejeitados sem enviar ordens e sem contaminar métricas live. Exponha agregados por período, símbolo, TF, playbook e motivo. Implemente retenção/índices para não sobrecarregar o Postgres. Faça apenas a API mínima; UI pode ser adiada. Adicione testes de classificação, dedup e separação shadow/live. Atualize log/status.
```

Aceite: é possível medir quantas operações cada filtro remove e o resultado posterior delas.

### Prompt 10 — Backtest realista

```text
Trabalhe somente no P10. Faça o backtest reproduzir o caminho live dos playbooks, gates, sizing e saídas sem look-ahead. Modele taxas maker/taker, funding, slippage por liquidez, latência do scan, maker não preenchido, fallback MARKET e fills parciais. Trate candles que tocam stop e alvo na mesma barra de forma conservadora ou use timeframe inferior quando disponível. Implemente walk-forward purgado, universo point-in-time e relatório por janela/regime. O fator fixo 0.70 deve permanecer apenas informativo. Adicione datasets sintéticos que detectem look-ahead e ambiguidade intrabar. Atualize log/status.
```

Aceite: backtest e shadow compartilham regras e diferenças conhecidas aparecem explicitamente no relatório.

### Prompt 11 — Learning e rotação

```text
Trabalhe somente no P11. Ajuste learning para combinar baseline longo com janelas recentes e decay temporal. Queda de edge pode reduzir risco rapidamente; aumento de risco exige amostra maior e estabilidade em múltiplas janelas. Ative edge decay inicialmente somente como redutor. Na rotação, exija consistência walk-forward, liquidez, amostra mínima por janela, quarentena shadow e controle de múltiplas comparações antes de auto-promover. Não use win rate isolado: use EV líquido com intervalo de incerteza. Preserve rollback por feature flags. Adicione testes de regime novo, amostra pequena, falso vencedor e promoção estável. Atualize log/status.
```

Aceite: amostra pequena ou performance antiga não aumenta risco nem promove símbolo automaticamente.

### Prompt 12 — Shadow e canário

```text
Trabalhe somente no P12. Não implemente nova estratégia. Crie checklist e relatório go/no-go comparando legacy versus V3 por playbook, TF, lado e regime. Exija no mínimo 100 trades shadow totais e 30 por playbook habilitado, salvo justificativa estatística mais conservadora. Reporte EV líquido, profit factor, drawdown, MAE/MFE, slippage, taxa de falha de proteção, duplicatas, rejeições e calibração. Prepare configuração de canário com risco máximo 0,25–0,35% por trade, risco aberto ≤1,25%, notional ≤50%, CT block e kill switch ensaiado. Faça teste documentado de rollback. Não habilite live automaticamente; entregue decisão go/no-go para aprovação humana. Atualize log/status.
```

Aceite: relatório reproduzível, rollback testado e nenhuma mudança automática para dinheiro real.

## 8. Ritual de acompanhamento

Ao final de cada dia de execução:

1. atualizar status, datas reais e bloqueios;
2. anexar commit/PR e testes;
3. registrar decisão e métricas no `HARDENING_LOG.md`;
4. recalcular apenas datas dos pacotes dependentes;
5. não iniciar pacote bloqueado;
6. marcar atraso quando a data atual superar o fim planejado e o status não for concluído.

Reuniões/checkpoints sugeridos:

- segunda-feira: planejamento e riscos da semana;
- quarta-feira: bloqueios e qualidade dos testes;
- sexta-feira: demonstração, métricas e decisão de avanço.

## 9. Política de mudança

- Uma variável alterada por experimento.
- Toda alteração de estratégia começa em shadow.
- Nenhuma promoção com base apenas em win rate.
- Nenhum sizing maior com amostra pequena.
- Mudança estrutural e ajuste de parâmetros não entram no mesmo experimento.
- Toda feature nova tem flag, observabilidade e caminho de rollback.
- O responsável humano aprova qualquer ativação live.
