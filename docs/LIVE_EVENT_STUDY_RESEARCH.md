# Réplica econômica por eventos — entrega local parcial

Base `c12ea043`, checkout principal autorizado. Trabalho de 12–13/10/2026.
**O lote integral NÃO está concluído.** A infraestrutura abaixo é verificável
localmente, mas não substitui a réplica exata solicitada para o primeiro estudo.
Nenhum estudo real, seleção candidata, coleta, SHADOW ou CANARY foi ativado.

## O que foi ligado

Manifesto `R13_LIVE_EVENT_MANIFEST_V1` → export bruto oficial + eventos próprios
por braço → seleção observada / Score V3 real → admissão e transporte isolados
→ gestor real isolado → ledger R05C/R05E → carteira causal → quatro dobras de
calibração e seis econômicas → verificador → CAS oficial → catálogo DRAFT.

Famílias novas: `CW_LIVE_EVENT_DATASET_V1`, `CW_LIVE_EVENT_EXECUTION_V1`,
`CW_LIVE_EVENT_LEDGER_V1`, `R13_LIVE_EVENT_STUDY_V1`,
`R13_LIVE_EVENT_EVIDENCE_V1` e `R12_PRE_SELECTION_EVENT_V1`.
Não são aliases de R10A/ArtifactV1. Seus payloads/hashes/semânticas continuam
intactos. A nova família é despachada antes dos validadores/loader antigos.

- Admissão usa `_compute_qty`, precisão e gates puros P03 reais, carregados do
  código, com parâmetros e carteira explícitos. MARKET, GTX, parcial/cancel e
  fallback `-mfb` têm recibos vinculados a pedidos/ordinais/causas exatos.
- Ordem aceita não prova fill. A identidade, quantidade, preço, taxa e relógio
  dos fills de entrada são confrontados com os usados pelo ledger e gestor.
- Saída normal confere COID, lado, símbolo, reduceOnly, quantidade enviada e
  instante da ação. Condicionais exigem vínculo ordem↔algo explícito e causal;
  ele não autentica a corretora. Proteção recusada não admite saída financeira.
- Seed do gestor confere quantidade, base, relógio, fase, alvos e IDs da
  proteção executada. Não envelhece artificialmente um trade para antecipar
  time-stop. BE/trail/autoheal/time-stop continuam no caller original.
- Ledger conserva quantidade, deduplica, rejeita conflito e diferencia fonte
  sintética/modelada/observada. Usa comissões/conversões/funding explícitos.
  Slippage embutido no fill não é descontado outra vez. R econômico usa o
  total com funding pelo denominador R05; R líquido sem funding fica separado.
- Carteira mantém reserva em envio incerto e exposição de entrada comprovada
  mesmo com falha posterior. Resultado futuro não financia entrada anterior.
  Sem ordem global comprovada, empate no milissegundo não libera dinheiro.
- Índice frio precede detalhes; IDs/tempos de holdout e material posterior ao
  corte são recusados antes de abrir/hashar seus corpos. Ausência não é zero.
- O modelo de execução suportado é uma tabela de consequências explicitamente
  fornecidas/vinculadas, não um simulador de fila, prioridade ou intravela.
  Uma hipótese contrafactual sem seus próprios recibos vira NON_COMPARABLE.

Hashes demonstram integridade, contexto e fontes locais, **não autenticidade**
dos fatos fornecidos. `TEST_ONLY` acompanha todas as camadas, inclusive modelos
e artefatos. Nenhum relatório concede autoridade operacional.

## Persistência e leitura

Namespace `r13evt:*` em `policy_simulation_state` e JSON existente. Reprodução
completa antes de gravar, fora da transação; publicação pelo CAS oficial.
Mesmo input não sobrescreve prova diferente. Reinício confere evidence_key,
identidade e corpo. Representação JSON é congelada antes do hash; tuples dos
motores nativos não viram conflito falso após roundtrip em JSONB.

O catálogo reutiliza lock/upsert/StrategyExperiment e cria somente DRAFT.
Seu validador estrutural continua puro: verificação de runtime/financeiro é
separada. O loader/ciclo operacional legado recusa esta família explicitamente.
O status existente `/api/strategy/p05/status` recebe `research_batch.event_replica`
(uma leitura adicional); GET não executa motor, fitting, bootstrap ou escrita.
O CLI explícito `research_pipeline.py --manifest ... --dataset-dir ...` lê
`event-dataset.json` para esta versão; preços/calibração R10A são recusados.
Nenhum comando real de pesquisa/persistência foi executado contra produção.

## Provas locais

As contagens finais e os comandos estão no checkpoint. A matriz contém
controle positivo por caller real, UNKNOWN/reserva, patrimônio causal,
seed/quantidade/relógio adulterados, fontes, JSONB, catálogo e holdout selado.
Exemplo integrado sintético: entrada 2×100, saída time-stop 2×102, taxa de
saída 0,20: líquido 3,80 USDT / 0,38R; com funding −0,50: total 3,30 / 0,33R.
Isso prova aritmética e vinculação local, **não lucro do bot na conta**.

PG16 descartável UTF-8/asyncpg, somente socket Unix: duas conexões e barreira
determinística, um vencedor CAS, repetição/reinício, conflito preservado,
catálogo DRAFT e GET com engines/writes proibidos. TCP/DNS contados em zero.
Sem migração/tabela/coluna/ENV/endpoint/scheduler novo, nem frontend alterado.

## Pendências técnicas — não chamar de falta de amostra

1. **Pyramiding ON:** portas e cadeia de segmentos foram completadas localmente
   em 14/10, após o preflight autorizado. LONG/SHORT percorrem entrada → gestor
   → reforço → gestor → ledger pelos callers reais isolados. Não há mais recusa
   genérica de "cadeia não implementada"; ausência de portas/limites e qualquer
   prova incompatível continuam recusadas. Detalhes em
   `LIVE_PYRAMID_EVENT_CHAIN.md`. A autoridade/persistência dessas portas é
   simulada com recibos vinculados, não prova de atomicidade com a conta.
2. **Sizing upstream:** a camada consumidora nativa foi integrada em 14/10:
   trecho original do caller e helpers/acessores reais, com equity e caches
   causais explícitos, sem reaplicar o canário. ENTRY que declara essa camada
   não pode omiti-la e cair no cálculo legado. Detalhes em
   `LIVE_UPSTREAM_SIZING_RESEARCH.md`. Ainda faltam a geração de elegibilidade,
   os escritores do aprendizado/decay e a geração dos leitores anteriores.
   Em 15/10, os produtores nominais e throttle de batch foram
   completados localmente, junto dos trechos filler de sort/rota/contadores;
   detalhes em `LIVE_SIZING_PRODUCERS_AND_BATCH.md`. A lista pós-build, tier
   final, dados dos leitores e retornos de persistência continuam observados.
   Isso não reproduz todo o scanner nem prova a geração de seu histórico;
   não alegamos equivalência integral do stack de decisão do bot.
3. **Exportações antigas com exclusões:** não conservam todos os IDs excluídos.
   A anexação recusa reduzir silenciosamente o denominador. Novo export precisa
   preservar essas identidades frias/UNKNOWN e a ordem causal antes de aceitar
   tal população como integral. Não houve backfill nem preço/resultado inventado.
4. **Carteira financeira integral:** o livro causal conserva exposição/reservas,
   mas não reproduz saldo Binance, caixa parcial, funding intratrade e P&L não
   realizado. Declara `broker_balance_replica=false`. O fechamento desses
   contratos ainda é necessário para chamar a réplica financeira de exata.

Após esses fechamentos: ratificar manifesto/par/custos/gestão/cortes completos,
autorizar eventual coleta separadamente e obter evidência prospectiva. A
aprovação anterior de hipótese não ativa flags nem ratifica automaticamente
este manifesto. Não forçar trade nem promover para demonstrar funcionamento.

LIVE, limites, alavancagem, champion, calibração, posições manuais e histórico
preservados. Maker/pyramiding/LIVE_SIZE_MULT intencionais não foram desligados.
Nenhuma ordem real, exchange, banco externo, Telegram, push ou deploy.
