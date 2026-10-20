# Checkpoint — gestão LIVE em pesquisa

12/10/2026 · checkout principal autorizado · base `80d7ece8`.

## Estado

`LOCAL_VERIFIED_RESEARCH_MANAGER_ONLY / WAITING_STUDY_INTEGRATION`.

Código novo: serviço de contrato/processo, adaptador isolado e duas suítes.
Nenhum serviço existente, frontend, banco, flag ou configuração foi alterado.
O pedido fechado inclui configuração completa, estado, identidade da fonte e
respostas vinculadas/causais. Previews sintéticos não são evidência.

## Provas já executadas

- 29 testes iniciais verdes antes da revisão cruzada.
- 44 focais verdes após fechamento dos campos/receipts/consultas/imports.
- 45 focais verdes após ampliar categorias de time-stop e UNKNOWN secundário.
- Primeira regressão completa: erro de montagem do comando (PYTHONPATH ausente,
  import de `services` não localizado). Não é prova de suíte completa verde.
- Fecho de carregamento direto da fonte integrado; controle em memória prova
  fonte NEW/cache OLD com mtime e tamanho iguais, sem apagar nem escrever cache.
- Estado final: **47 focais verdes 2×** (37 callers/contratos + 10 memória/loader).
- Regressão completa final: **2.958 executados, 2.956 aprovados, 2 skips** R05C
  históricos por fixture auditada privada ausente. Nenhum skip novo/fabricado.
- Sintaxe dos quatro Python aprovada; staged diff-check limpo; nenhum TS/TSX,
  migração ou import desta réplica pelo app. PostgreSQL não foi exercitado neste
  pacote: a persistência do adaptador é deliberadamente apenas em memória.

## Retomada exata

1. Implementar integração **versionada** no pipeline econômico e catálogo, sem
   admitir este artefato no contrato R10A/R13 antigo por renomeio.
2. Completar fontes/execução/carteira/ledger necessários ao estudo e provar
   paridade. Não atribuir a este motor pós-entrada simulação de maker/pyramiding.
3. Congelar dados/configuração/custos e ratificar manifesto inteiro; continuar
   sem estudo ou autorização LIVE até os requisitos estarem satisfeitos.
4. Este pacote fica em commit local com paths explícitos, sem pessoais.
   Não publicar pesquisa isolada com um restart desnecessário do bot;
   concentrar eventual publicação com a integração.

Comandos finais executados (Python 3.11.15, dependências já instaladas):

```sh
env -u DATABASE_URL -u BINANCE_API_KEY -u BINANCE_API_SECRET backend/.venv311/bin/python -B -m unittest discover -s backend/tests -p 'test_live_management_replica*.py'
env -u DATABASE_URL -u BINANCE_API_KEY -u BINANCE_API_SECRET PYTHONPATH=backend backend/.venv311/bin/python -B -m unittest discover -s backend/tests
```

## Continua bloqueado por implementação/insumos

Integração versionada no estudo econômico, entrada/execução/contabilidade e
custos, configuração efetiva e origem temporal dos eventos, manifesto ratificado.
Não houve estudo, fitting, outcome/holdout, promoção, CANARY ou alteração LIVE.
O documento preparatório de 08/10 é histórico, não autorização executável.
