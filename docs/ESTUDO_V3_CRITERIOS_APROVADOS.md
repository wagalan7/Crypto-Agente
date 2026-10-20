# Crypto Win — proposta de estudo V3 e política de aceite

Data: 07/10/2026. Código consultado: main, commit 2d6d87b1.

Estado: APROVADA PARA IMPLEMENTAÇÃO ANALÍTICA — SEM ATIVAÇÃO LIVE.

Aprovação humana nesta conversa em 07/10/2026: «ap;rovado», em resposta à
proposta apresentada. A aprovação abrange a candidata/critério e sua implementação;
não concede ratificação de manifesto com valores ainda pendentes, aprovação
SHADOW/PROMOTION/CANARY, acesso à conta, coleta ou execução de ordem.

Este documento é uma proposta, não um manifesto executável, aprovação de
experimento ou ordem para ativar flags. Prepará-lo não autoriza coleta, acesso
à conta, promoção, canário, cutover financeiro ou mudança no LIVE.

## 1. Proposta em linguagem simples

Comparar as escolhas do bot atual com uma única candidata que usa o Score V3
para selecionar e ordenar os mesmos sinais disponíveis. Pergunta principal:
essa seleção produz resultado líquido melhor, sem piorar o drawdown e sem
eliminar uma parcela excessiva das oportunidades do bot atual?

Medir também a frequência dos stops, as vitórias removidas e a quantidade de
operações. Reduzir stops por simplesmente deixar de operar não será tratado
como melhoria. Não há promessa de lucro, de menos stops ou de mais entradas.

Não substituir o núcleo estratégico, criar novos playbooks, ampliar o universo
ou mudar gestão e risco neste estudo. Essas seriam hipóteses diferentes.

Os valores novos abaixo são escolhas experimentais propostas para aprovação
antes dos resultados; não são parâmetros comprovadamente ótimos ou garantias
estatísticas. Os critérios já existentes não serão reduzidos.

## 2. Comparação proposta — item 1

| Elemento | Proposta |
| --- | --- |
| Baseline | Decisão do champion efetivamente observada na oportunidade, com a identidade da configuração que a produziu. Não reconstruir o passado pela configuração atual. |
| Escopo | SELECTION_ONLY; única mudança declarada: SCORE_MODEL. |
| Candidata | R08D_SCORE_V3_RESEARCH_V1, fórmula existente; não a ablação conf-only do R08A. |
| Corte inicial | Score V3 >= 70/100. Hipótese fixada antes dos resultados, não equivalente ao corte da V2. Sem testar uma grade de cortes. |
| Configuração V3 | include_composite_confluence=false; min_evidence_fraction=0.6; population=RESEARCH_SHADOW. Preservar a allowlist, pesos, normalizadores e caps existentes, todos cobertos pelo fingerprint. |
| Perfil de cálculo | TREND_PULLBACK, fixo neste manifesto. É um perfil de ponderação da V3, não um classificador automático nem prova de que todas as linhas são pullbacks. |
| Universo | Os símbolos, quote e timeframes efetivamente disponíveis e autorizados no momento do congelamento. Listagem explícita, sem ampliação automática e sem misturar USDT/USDC. |
| Gestão | A mesma configuração de replay nos dois lados, explicitamente congelada. Documentar diferenças em relação ao executor; defaults de replay não são prova de paridade com o LIVE. |
| Segurança operacional | Guards, geometria, proteção, margem, propriedade manual/BOT e limites existentes continuam obrigatórios. Nenhum veto de segurança é removido para fazer a candidata operar. |

Caps existentes da V3: regime/MTF 25; estrutura 20; gatilho 20; qualidade da
entrada 15; volume/liquidez 12; derivativos 8. São caps por categoria, não
probabilidades. Evidência mínima de 60% por linha e cobertura de 90% da coorte
medem coisas diferentes; ambas devem ser reportadas.

A V3 pode mudar o ranking, o timeframe vencedor e o setup escolhido. Isso não
autoriza recalcular stop/TP ou reclassificar tier. O replay da população bruta
não prova equivalência dos vetos do executor: a fidelidade prospectiva continua
sendo requisito independente antes de qualquer promoção.

### Conferências obrigatórias antes de gerar o manifesto real

1. Registrar a configuração efetiva do champion e as versões utilizadas; o
   commit local não demonstra quais variáveis estão carregadas em produção.
2. Conferir se universo, quote, gestão e perfil cabem no caminho implementado.
   Incompatibilidade bloqueia; não substituir silenciosamente por defaults.
3. Preencher custos numéricos com evidência e unidade explícitas. O replay
   implementado usa R10A_COST_CONFIG_BPS / DECLARED_MODEL: não apresentá-lo como
   ledger observado da conta. Taxas, slippage e funding precisam de valores e
   tratamento aprovados antes de abrir outcomes; ausência não vira zero.
4. Preencher datas de corte, janela, fontes dos preços/quotes e hashes. Preços
   atuais não substituem quotes históricos ponto-no-tempo.
5. Apresentar o manifesto final e seu bundle_hash para ratificação. A aprovação
   deste DRAFT não aprova valores de custos ou identidades ainda não conferidos.

Essas conferências estão pendentes. Este documento não inventa saldo, custos,
universo de produção, calibração ou hashes para parecer completo.

## 3. Protocolo temporal e evento

- Usar o export oficial da população pré-seleção; nenhuma fixture de teste vira
  evidência real. Ausência/erro de fonte bloqueia o estudo.
- Propor divisão cronológica 50% treino / 25% validação / 25% holdout. Fixar as
  fronteiras a partir de um índice sem outcomes, antes da avaliação, e registrá-las
  no manifesto. Não repartir por vitórias/perdas ou por conveniência do resultado.
- Usar as quatro dobras cronológicas já implementadas na validação. Labels
  precisam estar disponíveis no corte correspondente, sem sobreposição de
  oportunidades entre treino e OOS.
- Purga/embargo devem cobrir a janela completa de entrada e resolução, com sua
  unidade declarada. Não copiar um número de barras de outro timeframe.
- Não carregar ou avaliar outcomes do holdout para este aceite inicial. Seu
  eventual uso exige um plano separado, pré-registrado e autorizado.
- Não ajustar pesos, perfil ou corte 70 após olhar validação. Qualquer mudança
  material gera outra hipótese/manifesto; não reciclar o holdout como treino.

Evento proposto para a calibração: P_TP1_BEFORE_STOP. Horizonte e regra de
censura explícitos, derivados da gestão congelada. Não converter P(TP1) em
P(lucro), P(TP2) ou score/100. Expiração sem prova do alvo não vira fracasso
fabricado; censuras entram no relatório de qualidade e cobertura.

Validade proposta do artefato: 30 dias após sua geração, sempre limitada por
revogação e compatibilidade. Mudança de modelo/população/evento/horizonte ou
configuração relevante exige nova evidência e autorização vinculada; a validade
de 30 dias não dispensa nenhum mínimo de amostra ou observação.

## 4. Aceite da calibração — item 2A

Treino existente preservado: pelo menos 200 observações únicas utilizáveis e
30 labels por faixa fixa de dez pontos. Faixa fraca não herda vizinho, global,
V2, tier ou 0.5. Score 100 permanece na última faixa.

Proposta adicional de critérios OOS, ainda não implementada/aprovada:

| Critério | Limite proposto |
| --- | --- |
| OOS global | >=100 oportunidades únicas com previsão e label do evento compatível. |
| Suporte por faixa | >=30 labels OOS em cada faixa que se pretende autorizar. Sem suporte, a faixa permanece indisponível; não autorizar com dados de outra faixa. |
| Cobertura | >=90% da população elegível para o evento, contabilizando exclusões e censuras; não trocar o denominador para aumentar a cobertura. |
| Dobras | Quatro dobras executadas, >=20 previsões utilizáveis em cada uma. Ausência de uma dobra não vira resultado zero. |
| Erro médio de confiabilidade | <=5 pontos percentuais, ponderado por amostra OOS. |
| Erro por faixa autorizada | <=10 pontos percentuais entre probabilidade prevista e frequência observada OOS. Publicar n e Wilson 95% por faixa. |
| Brier comparativo | Melhora sobre uma previsão constante estimada exclusivamente no treino de cada dobra, avaliada nas mesmas linhas OOS. IC 95% do ganho agregado com limite inferior >0. |
| Consistência | Pelo menos três das quatro dobras não piores que a referência constante em Brier; nenhuma dobra com erro médio de confiabilidade >10 pontos percentuais. |

Brier não será usado sozinho: também serão exigidos confiabilidade, suporte,
cobertura e incerteza. A documentação oficial explica que Brier combina
calibração e discriminação, portanto menor Brier não demonstra isoladamente
melhor calibração. [Referência: scikit-learn, Probability calibration](https://scikit-learn.org/stable/modules/calibration.html).

Congelar método de incerteza: bootstrap por blocos de dia UTC, seed=7, 2.000
reamostragens e IC de 95%; o bloco mantém juntas as oportunidades do mesmo dia.
São regras propostas, não uma alegação de independência entre moedas ou de
garantia de robustez. Reportar sensibilidade à dependência; evidência insuficiente
não permite aprovação. O desenho cronológico é obrigatório porque observações
temporais não são intercambiáveis. [Referência: scikit-learn, Cross-validation](https://scikit-learn.org/stable/modules/cross_validation.html).

Avaliar métricas por dobra usando a probabilidade congelada antes de seu OOS;
na agregação por faixa, usar a média das previsões realmente emitidas, não a
probabilidade recalibrada depois. O último artefato não representa sozinho todas
as dobras. Reportar identidades e suporte do artefato que se pretende servir.

Resultado: ACCEPTED, REJECTED ou INSUFFICIENT_EVIDENCE, com checks e motivos.
Artefato inválido/expirado/revogado é indisponível. FITTED ou OOS_VALIDATED,
isoladamente, não satisfazem este aceite. Nenhum resultado concede licença LIVE.

## 5. Aceite econômico — item 2B

Avaliar separadamente o payoff da gestão completa, com parciais/runner/custos.
P(TP1) não é um modelo binário de todo o lucro da operação. Não calcular EV da
gestão inteira multiplicando P(TP1) pelo RR do alvo final.

Preservar os critérios existentes de GoNoGoCriteria:

| Critério existente | Exigência |
| --- | --- |
| Amostra Shadow | >=100 trades; >=30 por playbook habilitado. |
| Tempo | >=14 dias corridos e >=10 dias úteis completos. |
| Cobertura | >=90%. |
| Expectativa líquida | >=0.05R por trade. |
| Incerteza | <=0.05R, conforme contrato existente de cálculo. |
| Drawdown | <=8R. |
| Estabilidade | >=0.5, conforme definição do código existente. |
| Fidelidade | Divergência <=5%, com população/estágio/identidade conciliados. |
| Falhas/duplicatas/proteção | Zero falhas operacionais, zero duplicatas econômicas e zero falhas de proteção não resolvidas, com observação aplicável e cobertura comprovada. |

Exigir também o veredito existente de walk-forward favorável à candidata,
incluindo o delta da política inteira: operações comuns, removidas e adicionadas.
O IC e o delta total devem concordar. Não aprovar por uma subamostra favorável,
somar REAL ao SHADOW ou tratar desconhecido como prejuízo evitado.

Para a hipótese de redução de perdas, propor ainda: drawdown não pior que o da
baseline e preservação de pelo menos 70% da quantidade de trades da baseline
na mesma janela. Se a baseline não tiver trades comparáveis, a razão é
indisponível, não vitória da candidata. Critérios anteriores mais conservadores
que se apliquem continuam valendo.

Stops e vitórias removidos, taxa de stop por exposição e ritmo de operações
devem aparecer juntos. Melhor resultado de setups é evidência analítica, não
dinheiro realizado na conta. Vencedor não é obrigatório.

## 6. Decisão proposta sobre proteção

Propor aceitação de SHADOW_SIMULATED somente como evidência de proteção do
Shadow para a elegibilidade da primeira promoção cadastral, vinculada a este
estudo e ao contrato aprovado. Não liberar todos os estudos com uma constante
global nem deixar a decisão implícita na publicação do código.

Isso NÃO prova SL/TP real, NÃO autoriza reserva/alavancagem/POST e NÃO dispensa
aprovação CANARY própria, seletor explícito, guards ou proteção real na conta.
O aceite operacional continua exigindo observação de execução real legítima.

Enquanto essa decisão não for aprovada, o gate de promoção permanece NO_GO.
Rejeitá-la mantém o bloqueio e exige definir uma trilha de evidência real; não
forçar uma entrada para contornar a falta de evidência.

## 7. Pacote de implementação após aprovação

1. Gerar o manifesto real com as conferências da seção 2; validar isolamento
   de SCORE_MODEL e calcular identidades antes dos resultados.
2. Implementar política de aceite versionada e dois registros separados:
   calibração aceita e economia aceita. Usar persistência oficial existente,
   sem motor/catálogo paralelo e sem DDL por conveniência.
3. Cada registro vincula versão/hash dos critérios, modelo/artefato, manifesto,
   dataset, preços, custos, evento/horizonte, escopo, artefato de evidência,
   autoridade/referência, validade e revogação. Drift invalida o aceite.
4. Preservar a semântica dos artefatos V1. Não forçar economically_approved=true
   num schema que o rejeita, nem re-hashear legado para fazê-lo passar. O aceite
   separado deve ser consumido explicitamente nos caminhos pertinentes.
5. Conectar criação/verificação do bundle, promoção, autoridade CANARY e suas
   revalidações ao mesmo contrato. Hoje PROMOTION/CANARY conferem OOS_VALIDATED:
   avaliação executada não poderá substituir os dois aceites.
6. Manter SHADOW observacional separado de licença de execução. Permitir coleta
   de evidência sem exigir previamente uma aprovação econômica impossível;
   nunca usar contexto SHADOW como autoridade de ordem.
7. Substituir a decisão global de proteção por autorização de escopo vinculada
   ao estudo, mantendo ausência/legado bloqueados. Não relaxar proteção real.
8. Expor estado/motivos nas leituras oficiais e atualizar a documentação.
   Nenhum endpoint de ativação automática, loop paralelo ou promoção automática.

Provas mínimas: DRAFT/TEST_ONLY não autorizam; OOS executado mas reprovado não
promove; aceite íntegro passa somente o propósito aprovado; alteração de
modelo/custo/dataset/critério recusa; faixa fraca/erro/expiração/revogação recusa;
mesma solicitação é idempotente; concorrência/restart preservam identidade;
SHADOW nunca autoriza POST; guards manuais/P03/risco e proteção BOT permanecem;
paridade LEGACY preservada. Testes sintéticos não viram evidencia operacional.

Agrupar essas mudanças num pacote revisado e uma publicação, se autorizada.
Aprovar este DRAFT não ativa flags, gera aprovação CANARY ou muda o LIVE.

## 8. O que significa concluir os itens 1 e 2

Item 1: proposta aprovada, conferências técnicas feitas e manifesto final
ratificado com configuração/custos/datas/identidades verificáveis.

Item 2: critérios aprovados, contrato de aceite implementado, conectado aos
consumidores reais e testado. Isso encerra a implementação da política, não
garante que a candidata tenha dados ou desempenho para passar.

Se a evidência real faltar, o estado correto continua sendo aguardando dados.
Se a candidata reprovar, o champion permanece. Não repetir mudanças para
produzir artificialmente um vencedor.

## 9. Aprovações ainda necessárias

- Proposta da candidata: V3 existente, corte 70, perfil fixo TREND_PULLBACK,
  seleção/ranking sobre sinais existentes; nenhuma nova estratégia de núcleo.
- Critérios novos de calibração OOS e validade de 30 dias da seção 4.
- Preservação dos gates econômicos e critérios comparativos da seção 5.
- Alcance limitado da proteção simulada da seção 6.
- Implementação analítica/governada sem ativação LIVE.
- Ratificação posterior do manifesto preenchido, antes de rodar estudo real.

Status: critérios e implementação analítica aprovados nesta conversa.
Ratificação do manifesto preenchido e autorizações operacionais continuam pendentes.
As referências a DRAFT descrevem a proposta original; não são licença de operação.

Fontes locais consultadas: research_manifest_service.py,
research_selection_service.py, score_v3_service.py,
score_v3_calibration_service.py, research_study_service.py,
preselection_experiment_service.py, walk_forward_service.py,
operational_governance_service.py e prospective_shadow_service.py.

Nenhum arquivo de produção, flag, conta, ordem, pausa, aprovação persistida,
histórico ou holdout foi alterado para preparar esta proposta.
