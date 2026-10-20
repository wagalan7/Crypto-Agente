# Lote 01 — correção das fronteiras de prova e retry

Data: 03/10/2026. Baseline: `ceb42af759e8799a091ac6c5beb0ab3d3d374fa6`.
Worktree autorizado: `blissful-sinoussi-0511a5`, branch
`claude/blissful-sinoussi-0511a5`. Integração em main, publicação e cutover
continuam separados. Único serviço alterado: `execution_accounting_service.py`.

## Três defeitos reproduzidos e corrigidos

1. **Referência incompatível depois de persistida.** Um hash íntegro não
   certificava a semântica de `source_ref`: moeda/símbolo/exchange/evento/janela
   incompatíveis podiam confirmar dinheiro após recalcular o hash. Agora a mesma
   `validate_commission_source_ref` roda no construtor e no veredito consumido
   pelo merge sob bloqueio e pela finalização. Ela exige identidade esperada
   derivada do fill, COMMISSION, trade/transaction IDs, moeda de liquidação,
   vínculo explícito com ativo e quantidade da comissão, valor não positivo e
   carimbos integrais válidos. O carimbo da observação integra o hash de
   integridade. Cada linha conserva a janela da **requisição efetiva**, inclusive
   cursor de paginação; uma janela agregada não legitima evento fora da página.
2. **Escala decimal tratada como divergência econômica.** `-0.42` e
   `-0.42000000` podiam criar CONFLICT e retirar P&L conhecido. Os números da
   referência material agora usam Decimal canônico; identificadores continuam
   textuais. Duas representações do mesmo valor são idempotentes nos dois
   sentidos. Mudança real de dinheiro continua conflitando e preserva a prova
   original. Janelas e carimbos de observação não viram identidade econômica.
3. **Resposta atrasada reabrindo retry encerrado.** O merge preservava oito
   conversões úteis de um lote de 49, mas também zerava seis tentativas e
   ressuscitava FAILED. Agora `retry_halted` é persistido no JSON existente,
   reconferido na finalização, no merge e na elegibilidade. Evidência atrasada
   contribui eventos, não autoridade para reabrir. Attempts/erro são preservados,
   `next_retry_at=None` e FAILED permanece fora da seleção de retry. O replay
   A→B→A não incrementa geração ou tentativas. Histórico de IDs limitado a 64,
   geração exata obrigatória e detecção de mudança real protegem também respostas
   antigas fora desse histórico.

Mesmo uma resposta tardia completa não auto-resume uma linha FAILED: recuperação
supervisionada permanece fora deste pacote. Não foi criado endpoint de recovery.

## Evidência local executada

- RED na baseline: **27 falhas** nos dez novos testes de fronteira, sem alterar
  o código sob teste. GREEN: **66 testes focais, 2×** (27 anteriores + 29 da
  correção conjunta + 10 novos), com controles positivos e negativos.
- PostgreSQL 16 real descartável, driver async, socket Unix e TCP/DNS bloqueados:
  **31 verificações, 2×, em bancos novos independentes**. As 21 anteriores foram
  preservadas. Dez novas verificam ambas as ordens de merge decimal, referências
  re-hasheadas inválidas, replay não adjacente e FAILED produzido pelo ciclo real
  seguido de oito provas atrasadas. Duas conexões exercitam a espera de row lock.
  P&L projetado, inclusão/exclusão no total e seleção de pendentes conferidos.
- Regressão `run_pg_r05c.sh`: **R05C_PG_INTEGRATION_OK**.
- Suíte completa: **2.566 executados, 2.564 aprovados, 2 skips históricos R05C**
  por fixture privada indisponível; nenhuma fixture financeira foi inventada.
- `py_compile` e `git diff --check` aprovados. Nenhum TS/TSX alterado.

Os helpers de prova dos testes antigos receberam os novos campos de vínculo,
sem relaxar assertions. Duas falhas de preparação foram corrigidas/declaradas:
a fixture PG de 49 fills usava conta diferente da linha; uma repetição na mesma
base deixava a linha FAILED do ensaio anterior e alterava a contagem global.
A repetição final usa banco novo, sem enfraquecer critérios nem mascarar estado.

## Compatibilidade e limites do aceite

Contrato V2 endurecido **antes de publicação**: provas antigas sem vínculo ou
carimbo íntegro são indisponíveis, preservadas para diagnóstico, nunca
re-hasheadas/promovidas automaticamente. V1 e NULL legado permanecem não
verificados. Sem backfill, DDL, tabela, coluna, ENV ou flag nova.

Estado: **LOCAL_VERIFIED** para estes casos; não é ausência demonstrada de bugs.
**WAITING_SOURCE** permanece quando o ledger não oferece vínculo inequívoco da
comissão estrangeira; não se fabrica conversão nem se assume paridade de moedas.
**WAITING_OPERATIONAL_OBSERVATION** permanece: nenhuma entrada real observada.
Estratégia, sizing, limites, alavancagem, pausas, posição manual, P03/P04 e flags
financeiras inalterados. Sem conta real, banco externo, ordem, mensagem, merge,
push, deploy ou cutover nesta execução.
