# Calibração V2 — cobertura 75–100 com mínimo por faixa

Base `c85fc76e`, checkout principal `main`.

## Autorização e efeito operacional

O usuário autorizou explicitamente alterar a calibração consumida pelo bot LIVE
para scores 75–100, exigir 30 observações por faixa e bloquear faixas
insuficientes. Também reconheceu que novas probabilidades podem mudar entradas
e dimensionamento pela política existente, sem elevar limites de risco ou
alavancagem.

A autorização anterior genérica não havia liberado o patch. Nesta entrega, a
primeira revisão de permissão expirou sem aplicar nada; a repetição permitida,
com a autorização detalhada, foi aprovada. Não houve contorno de permissão.

## Antes e depois

Antes, V2 terminava em `[63,75)`: score 75 ou maior não tinha probabilidade
individual quando havia tabela, enquanto cache ausente seguia o comportamento
legado de `CALIBRATION_UNAVAILABLE`.

Agora os nove bins anteriores permanecem nas mesmas bordas, seguidos de:

- `[75,80)`, `[80,85)`, `[85,90)`, `[90,95)` e `[95,100]`.
- A borda interna exclusiva 100.1 inclui 100; scores superiores a 100 são
  recusados no lookup V2 e não contam para o último bin.
- Cada faixa nova exige seu próprio `n_total >= 30`. Total global alto,
  probabilidade global, prior e observações de outro bin não cumprem esse mínimo.
- Sem cache, bins ou amostra global, V2 alto também fica bloqueado.

`INSUFFICIENT_BIN_EVIDENCE` é bloqueante e entrega `prob_tp1=None` e
`prob_tp2=None`, nunca zero fabricado nem fallback global. Contagem malformada
(ausente, bool, string, negativa, fracionária, não finita ou acima do total)
invalida o contrato. Não foram relaxados os critérios preexistentes de fórmula,
fingerprint, probabilidade em [0,1] e P(TP2) ≤ P(TP1).

## Matemática, identidade e consumidores

Reusados shrinkage e PAV existentes. Bins novos vazios recebem peso zero no PAV:
não deslocam as probabilidades dos bins antigos por mera adição de um prior
sem observações. Com evidência na cauda, o ajuste monotônico pode alterar também
probabilidades de outras faixas — não se afirma paridade numérica nesse caso.

O fingerprint já existente cobre todas as bordas; a partição estendida recebe
outra identidade. Tabela antiga não é reinterpretada por índice novo. Para V2
alto, o lookup exige a partição estendida exata, não um bin amplo construído
para somar amostras de contextos diferentes.

`bin_sample_count` foi acrescentado à proveniência JSON existente. Contrato
READY estendido exige contagem válida no próprio bin. No caminho de execução,
o score alto precisa corresponder ao índice e fingerprint atuais: READY antigo
ou de outra faixa não passa. Payload antigo `CALIBRATION_UNAVAILABLE` também
não libera V2 alto. Leitura do histórico continua sem recalcular probabilidades.

Os três helpers legados async/sync agora delegam o lookup validado em vez de
indexar diretamente pelo array global. Ainda presumem fórmula ativa; nenhum
consumidor operacional novo foi criado para eles.

O veredito central, o executor, snapshots e Kelly consultivo continuam nos
caminhos existentes. O painel mostra mensagem específica de amostra
insuficiente, sem chamar isso de incompatibilidade de fórmula.

## Limites e dados

- Partição LEGACY e política de indisponibilidade abaixo de 75 não ampliadas.
- Nenhuma fórmula de score, corte de tier, stop, TP, limite financeiro,
  alavancagem, flag, ENV, scheduler, endpoint ou schema de banco alterado.
- Os dados continuam sendo os pares da calibração existente: snapshots SHADOW
  resolvidos, com seed/backtest somente se já configurados pelos mecanismos
  anteriores. Não foi ativada nenhuma fonte ou refeita a classificação histórica.
- A proveniência individual histórica continua declarada como não versionada;
  o mínimo de 30 não prova lucro, independência estatística ou qualidade de fill.
- Nenhuma consulta à exchange, conta, banco de produção ou holdout nesta
  implementação/testes. Não foi executada recalibração remota.

## Testes e manutenção

16 testes específicos sintéticos: fronteiras, 0/1/29/30 observações, separação
por bin, contagens inválidas, cache ausente/antigo, fingerprint, mismatch,
contrato READY, preservação LEGACY, veredito real compartilhado e texto da UI.
O conjunto básico foi RED antes do patch. Um teste adicional demonstrou que
READY antigo/outra faixa passava; o gate do consumidor corrigiu esse caso.

Testes antigos atualizados apenas onde caracterizavam o contrato substituído:

- R06A: fórmula LEGACY continua incompatível com V2, mesmo com faixa numérica
  sobreposta; substitui a caracterização de que 80–99 sempre estavam fora.
- R06B2.1: verifica preservação do prefixo original de nove bins e das bordas
  LEGACY, além dos cinco novos bins, mantendo shrinkage e mínimo global.
- R06B2: schema da proveniência inclui contagem; teste de texto verifica o
  bloco JSX delimitado, não os primeiros 1.400 caracteres.

Um import de `patch` faltante no teste R06A adaptado foi corrigido antes da
validação final. Nenhuma fixture privada foi fabricada para eliminar skips.

Resultado final: suíte completa 2×, cada uma com 1.679 testes executados,
1.677 aprovados e 2 skips R05C preexistentes (fixture auditada indisponível).
`py_compile`, `tsc --noEmit` e `git diff --check` aprovados. Serviços de execução,
risco, portfólio, gerenciamento, recomendação, exchange, DB e main não receberam
alterações neste commit; o efeito operacional autorizado vem da calibração.

## Publicação

Entrega local, sem push/deploy. Publicar somente o commit revisado e verificar
o novo fingerprint, contagens por bin, contratos/skip reasons e saúde do
serviço. A regra usa o próximo cálculo de calibração pelo fluxo/cache existente;
não exige mudar ENV. Faixa com menos de 30 permanece bloqueada após publicar.
Não forçar entrada nem reduzir risco mínimo/notional ou outros gates para
produzir uma operação. O bloqueio de sizing diagnosticado é independente.
