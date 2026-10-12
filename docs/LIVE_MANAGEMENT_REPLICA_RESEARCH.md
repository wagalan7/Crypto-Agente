# Réplica de decisões da gestão LIVE — pesquisa local

Data: 12/10/2026. Base: `80d7ece81c92bcb21211494d4cb39a11060468de`.
Autorização: implementar pesquisa após a escolha humana de exigir réplica da
gestão LIVE antes do estudo. Não autoriza estudo real, coleta, SHADOW, CANARY,
alteração de variáveis, conta ou ordens.

## O que este pacote entrega

O motor `CW_LIVE_MANAGEMENT_EVENT_REPLICA_V1` chama a função **real**
`trade_manager_service._process_trade`, seus helpers e os cálculos reais de
indicadores/estrutura, em um processo isolado. Não copia fórmulas nem altera
qualquer arquivo de produção existente.

Escopo: **decisões de gestão de um trade BOT/managed já aberto e com entrada
confirmada**, na Binance, sob configuração e observações explícitas.
Não é réplica financeira completa da conta, motor de preenchimento de ordens,
entrada maker/fallback/pyramiding ou replay econômico integrado ao R13.

Reproduz a ordem atual: propriedade → posição → time-stop → autoheal → proteção
pré-TP1 → poeira/flat → parcial TP1 → runner/trailing. O objeto do caller e o
registro em memória são distintos, como nas sessões reais. Transições não são
antecipadas para facilitar o ensaio.

## Fronteiras de verdade

- Configuração: todos os 22 parâmetros usados pela gestão são obrigatórios,
  sem descobrir defaults ou copiar ENV da produção. O domínio admitido é
  explícito; entrada fora dele é recusada, não saneada como oportunidade válida.
- Estado: preços, qty inicial/restante, fase, IDs, prefixo, abertura **e** criação,
  contexto adaptativo e TF são fornecidos. Não há consulta ao banco real.
  O TF é uma fixture da snapshot ligada; não representa um vínculo real.
- Eventos: ticks ordenados, IDs únicos, relógios de observação/disponibilidade e
  filas de respostas explícitas. Não se cria sequência intravela a partir de
  OHLCV. Ausência de resposta vira lacuna, mesmo se o caller LIVE captura o erro.
- Cada resposta `BOUND` vincula a chamada exata (`request_hash`), seu índice
  global e a última ação anterior. `force=True` não admite posição observada
  antes da ação cuja consequência está sendo conferida. Empates de milissegundo
  exigem a ordem causal declarada pelos índices.
- `SYNTHETIC_PREVIEW` existe apenas para montar fixtures de teste. Sempre retorna
  `INCOMPLETE_TRACE`; é proibido para `OPERATOR_EVENT_TRACE`. Os testes fazem uma
  segunda execução com bindings fechados e verificam valores/ordem esperados.
- Hash é integridade, **não assinatura** nem comprovação de que alguém observou
  a exchange. Um trace de operador ainda precisa de origem auditável no estudo.
- Aceite de ordem não altera a posição. Fechamento em memória exige resposta
  explícita `close_trade.applied=true`. Proteção/pausa/notificação são intenções
  locais ou respostas fornecidas; não comprovam efeitos externos reais.
- As estimativas contábeis do caller são separadas como **não verificadas**.
  Não substituem R05C/ledger. `economics=null`, `live_equivalent=false`,
  `promotable=false` e `authorizes_live=false` em qualquer resultado.

`COMPLETE_TRACE` significa somente que as interfaces necessárias receberam
respostas vinculadas, sem lacunas do adaptador. Não significa posição protegida
na corretora, lucro conhecido, candidato aprovado ou aceite operacional.
Uma resposta explícita UNKNOWN pode constituir um trace completo cujo caller
legitimamente não decide nada. Respostas não consumidas ficam visíveis.

## Isolamento e reprodução

O subprocesso recebe apenas `PYTHONHASHSEED=0`, nunca o ambiente/credenciais do
parent. Serviços externos são substituídos antes de importar os callers.
TCP/UDP/DNS são bloqueados e contados; AF_UNIX só permite a infraestrutura local
do loop assíncrono, sem conectar banco. Não existe engine ou conexão SQL.

A memória aceita somente os dois SELECTs reais e seus IDs exatos: o trade e
o TF da snapshot fixture. Qualquer outra query/dependência deixa uma lacuna;
falha capturada pelo LIVE não apaga essa evidência. Commit e saída de sessão
registram o que realmente ocorreu em memória.

O fingerprint inclui manager, indicadores, estrutura, modelos, contrato,
adaptador e versões Python/SQLAlchemy/pandas/numpy/ta/Pydantic/Pydantic-core.
É conferido antes e depois dos imports/execução e no parent. Módulos locais
admitidos são carregados diretamente da fonte, sem usar bytecode pré-existente.
Uma versão diferente invalida o pedido; não é migrada silenciosamente.

## Comportamentos preservados, não “corrigidos” pela réplica

- TP1 é detectado pelo corte inclusivo de 60% da qty inicial, mesmo com parcial
  adaptativa. A fração adaptativa influencia outros cálculos, não esse corte.
- Runner começa pelo corte global configurado; trail exige movimento
  estritamente maior que o passo mínimo e stop do lado seguro do mark.
- Time-stop acontece antes de reconhecer uma parcial no mesmo ciclo e não
  impõe um limite novo às fases pós-TP1/runner.
- UNKNOWN na primeira posição impede gestão; UNKNOWN numa segunda leitura
  da transição BE pode usar o fallback de qty/entry do DB. Isso é comportamento
  atual, não garantia de que todo UNKNOWN sempre interrompe o LIVE.
- A graça do autoheal usa criação ou, quando ausente, abertura; o prefixo é
  preservado para cleanup mesmo sem IDs exatos.
- O código atual normaliza `1M` para `1m` na categoria do time-stop. O teste
  registra essa convenção; este pacote não a altera nem recomenda adotá-la.

## Execução manual local

Usar o Python já instalado no projeto; não instalar bibliotecas nem configurar
credenciais. O pedido deve obedecer aos schemas fechados do serviço.

```sh
backend/.venv311/bin/python -I -B backend/services/live_management_replica_service.py < TRACE.json
```

Não há endpoint, import pelo app, scheduler, fila ou loop de produção. O arquivo
`scripts/live_management_replica_worker.py` é o adaptador de uma chamada manual,
não um novo serviço Railway. O replay R10A e os manifestos/aceites legados
continuam recusando este motor. Não renomear a saída como R10A para contornar.

## O que ainda falta antes do estudo escolhido

1. Integrar este motor por contrato **versionado** ao export/replay, comparação,
   walk-forward, catálogo e política de aceite. A integração é código pendente,
   não simplesmente espera por dados. Preservar os contratos legados.
2. Completar entrada/execução, admissão de carteira e contabilidade/custos
   necessários à comparação econômica. Maker/pyramiding intencionais não são
   desligados nem declarados simulados por este pacote pós-entrada.
3. Congelar a configuração efetiva, contexto por trade, origem/cronologia dos
   eventos e identidade do champion/população. Casos não determináveis ficam
   não comparáveis; nenhuma trajetória, custo zero ou fill é inventado.
4. Fechar e ratificar o manifesto inteiro. Só então obter as autorizações
   específicas de coleta/SHADOW/estudo. Promoção/CANARY seguem separadas.

O requisito humano de réplica antes do estudo **ainda não está integralmente
satisfeito**. Este pacote fecha uma parte verificável dele. Nenhum outcome real,
holdout, banco/exchange externo, conta manual, mensagem ou variável foi usado.
Ver estado das provas e retomada em `LIVE_MANAGEMENT_REPLICA_CHECKPOINT.md`.
