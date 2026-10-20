# Estudo V3 — implementação dos critérios de aceite

Data: 08/10/2026. Base: `2d6d87b1f43d384b4a5bbbd29bd4425facbbd40b`.
Local: worktree autorizado `lote02-pesquisa`, branch `worktree-lote02-pesquisa`.

## Escopo e autoridade

A aprovação de 07/10/2026 autoriza implementar a proposta analítica de
`ESTUDO_V3_CRITERIOS_APROVADOS.md`, não ativar a candidata. Este pacote não
ratifica manifesto real, não concede aprovação SHADOW/PROMOTION/CANARY e não
acessa produção. Champion, parâmetros operacionais, limites, posições manuais,
histórico e holdout permanecem preservados.

A hipótese permanece `SELECTION_ONLY` / `SCORE_MODEL`, Score V3 existente,
corte 70, perfil fixo `TREND_PULLBACK`, confluência composta desligada e
evidência mínima de 0,6. Perfil de ponderação não é classificador de regime.
Não foram introduzidos novos sinais, playbooks ou alterações de stop/TP/sizing.

## Contratos e fronteiras de confiança

- `R13_V3_ACCEPTANCE_POLICY_V1`: política congelada, hash e critérios separados.
- `R13_RESEARCH_ACCEPTANCE_V1`: recibo com aceite de calibração, economia e
  proteção, identidade, origem, validade, revogação e motivos independentes.
- `R13_ACCEPTANCE_PROSPECTIVE_V2`: corte prospectivo com compromissos por
  oportunidade, denominador conciliado e identidade da coorte oficial.
- `R13_APPROVAL_VALIDITY_V2`: PROMOTION/CANARY vinculam o bundle **e** o hash
  do recibo exato. SHADOW mantém a identidade anterior para permitir coleta.

SHA-256 público prova integridade, não assinatura/autenticidade. A fronteira
de confiança é a produção oficial e a persistência controlada: recálculo
determinístico completo é obrigatório antes de gravar. Não existe endpoint
que aceite certificado arbitrário do cliente como autoridade. Leituras e a
borda de execução só verificam recibos dessa fonte; não fazem fitting, replay
ou bootstrap. Nenhum segredo efêmero, ENV, tabela ou coluna foi criado.

V1 de calibração continua com sua semântica anterior. `FITTED` e
`OOS_VALIDATED` não são aceites econômicos. Legado é legível, mas não ganha
aprovação por renomeio, re-hash ou conversão silenciosa.

## Produção das evidências

O export/replay/fitting oficiais são reutilizados. O treino de cada dobra
emite previsão e referência constante **antes** do seu OOS. Os IDs,
timestamps, extremos de treino, previsões, labels, faixas e denominador bruto
são conciliados com o índice oficial. Exclusão, censura, purga ou ausência de
índice não são ocultadas por um denominador limpo.

Calibração usa quatro dobras cronológicas; bootstrap por dia UTC, seed 7,
2.000 amostras e IC95%. Mantém 200 únicas no treino, 30 por faixa, 100 OOS,
30 OOS por faixa autorizada, cobertura 90%, ECE 5pp, erro por faixa 10pp,
consistência em 3/4 e limite inferior positivo do ganho de Brier. Score100
permanece na última faixa. Um único bloco temporal não demonstra incerteza.

O protocolo econômico fica explícito e hasheado antes do resultado: seis
dobras, CI existente por blocos de cinco deltas, 500 reamostragens e seed7.
Quatro deltas não satisfazem esse estimador de blocos; não se reduz o bloco
nem se fabrica um IC para aprovar. Este protocolo distinto do de calibração
deve constar da ratificação do manifesto real, que continua pendente.

Economia mantém todos os gates existentes, delta da política inteira,
drawdown não pior que a baseline e preservação mínima de 70% das operações.
Custos são modelo BPS declarado, não ledger observado. Ausência de fidelidade
prospectiva impede aceite: recomputar a candidata duas vezes ou compará-la ao
champion não substitui observação do runtime. REAL e SHADOW não são somados.

## Preparação administrativa e ciclo governado

A rota administrativa existente `operational-bundle` continua registrando o
bundle. Com os dois campos adicionais fechados `prospective_cutoff_ms` e
`expected_generation`, prepara um recibo da coorte oficial:

1. Transação sob a lock oficial: lê estudo/bundle/coorte e aprovação SHADOW.
2. Fora da transação: produz snapshot, métricas e recálculo completo do aceite.
3. Nova transação: reconfirma geração, estudo, start, recibo anterior e coorte;
   revalida SHADOW **depois** da última leitura; grava por CAS.

Não é avaliação automática no GET. Mesmo corte é idempotente; drift é recusado.
Recibo insuficiente/reprovado não avança a geração que mantém SHADOW coletando.
Tem revisão própria e histórico limitado. Aceite congelado avança a geração
uma vez e não é trocado sem outra decisão. Não há reaprovação automática.

V2 congela identidade de todas as linhas e observações efetivamente usadas.
Uma linha excluída/pendente pode resolver após o corte sem entrar nos números
antigos ou invalidá-los por progresso normal. Alterar uma linha usada, a
identidade congelada, o conjunto de oportunidades ou a observação antes do
corte continua bloqueando. O recibo não é reescrito pelo resolver.

PROMOTION exige dois aceites, proteção de escopo e aprovação humana exata.
`SHADOW_SIMULATED` prova somente proteção simulada para a primeira promoção
cadastral; não prova SL na corretora, não reserva margem nem autoriza POST.
CANARY continua exigindo aprovação própria, seletor explícito e todos os
guards reais. SHADOW nunca é autoridade de ordem.

Revogação/drift do estudo persistido é conferido novamente na autoridade.
Relógio e fence são revalidados após leituras/rollback/cleanup. Expiração no
instante final é indisponibilidade, não autorização antiga ressuscitada.

## Verificação e preservação

As contagens finais e comandos executados estão no checkpoint. Os testes
sintéticos demonstram contratos, não melhora de lucro ou redução de stops.
PostgreSQL de QA é descartável, UTF-8, socket Unix; TCP/DNS bloqueados e contados.
A exceção privada de identidade TEST_ONLY só existe no harness e nunca reduz
limites, substitui métricas ou fabrica aprovação real.

No harness governado, o relatório sintético grande e a instrumentação de
concorrência excederam o orçamento de captura de 1,5s. O mesmo caller oficial
foi exercitado com orçamento **exclusivo de QA** de 30s; o orçamento de produção
não mudou. Isso não mede nem aprova latência de produção. O controle integrado
de relógio expira recibo, artefato e aprovação CANARY conjuntamente; a prova
isolada da expiração do recibo pertence ao harness de aceite. A resolução
tardia da linha excluída foi reconfirmada no PG como terminal e posterior ao
corte, não apenas pela chamada ao resolver.

Sem DDL, frontend/dist, dependências novas, worker, scheduler, endpoint novo,
ordem, Telegram, acesso à Binance ou banco externo. Nenhuma flag foi ativada.
Main e o worktree de outro aplicativo não foram editados. Dois prompts
preexistentes permanecem fora do stage. Publicação não faz parte da prova local.

## O que ainda falta para o estudo real

1. Conferir config/versões efetivas, universo/quote, gestão, custos/fonte,
   datas, protocolo temporal e hashes; ratificar o manifesto preenchido.
2. Autorizar a coleta/SHADOW específica e acumular amostra prospectiva suficiente.
3. Executar estudo sem abrir o holdout; observar se a candidata passa ou reprova.
4. Somente se passar: decisão de promoção e CANARY explícitas, depois observação
   operacional legítima. Não forçar operação para produzir evidência.

Conclusão técnica do pacote não conclui os passos externos, não declara
`OPERATIONAL_ACCEPTED` e não garante ausência de defeitos.
