# Operação e clareza do scanner — 15/10/2026

## Diagnóstico observado, não hipótese de estratégia

O backend estava publicado em `80d7ece8`, de 08/10; havia seis commits locais
posteriores ainda não publicados. O scanner e o reconciliador estavam ativos.
O registro do app não mostrava novas entradas automáticas desde 25/08 UTC.
Esse registro não é o inventário da exchange.

Logs recentes de admissão mostraram `BLOCKED_CAPACITY` com causa
`MANUAL_ACK_PROOF_STALE`. A leitura administrativa comprovou reconhecimento
APE em `INVALIDATED`, por identidade divergente — não apenas TTL expirado.
Contadores P03 zerados e risco sem pausa não revelavam essa causa independente.
Isso explica as tentativas recentes observadas, não prova a causa de todos os
50 dias. Rejeição não equivale a oportunidade lucrativa perdida.

O proprietário confirmou novamente que a posição atual era manual e autorizou
novo reconhecimento. Executado somente o POST administrativo oficial, após
inventário fresco: `MANUAL_ACK_OK`. O ciclo oficial publicou prova `ACTIVE`,
revisão validada coincidente e escopo `ACCOUNT`. Nenhuma ordem foi forçada,
nenhum reconhecimento foi convertido em `managed`, nenhum gate foi removido.

## Correção implementada

- Veredito específico da prova manual, mantendo o predicado de autorização.
  Paridade independente contra o HEAD original: 738 combinações equivalentes.
- Resumo público saneado em `/api/execution-incidents/status`: só qualidade,
  bloqueio, motivo e escopo `MANUAL_VALIDATION_ONLY`. Sem conta, posição,
  fingerprint, credencial ou I/O de exchange. Duas leituras DB adicionais por
  chamada. Falha é desconhecida; nunca liberação. Não é autorização atômica.
- Histórico de rejeições preserva o código específico da admissão em vez de
  exibir apenas capacidade. Não altera a decisão nem reenvia a entrada.
- Home/Sistema distinguem pendência manual de incidente e não oferecem um
  botão de retomada que não resolveria a causa.

## Interface

- Scanner distingue viés de ordem. Lista selecionável por teclado, busca,
  ordenação com os mesmos critérios e hierarquia visual mais simples.
- Resumo antes dos detalhes: preço desconhecido, neutro, plano incoerente,
  stop/TP1 ultrapassado, fora/na zona ou referência de mercado explícita.
  Falta de zona não vira entrada a mercado. `signal.entry` não vira cotação.
- Força heurística `/100` não é probabilidade; scanner não inventa calibração.
  Recomendações só mostram probabilidade finita compatível com proveniência
  READY; TP2 inconsistente não aparece. Nenhum valor numérico é recalculado.
- Tier/qualidade parcial não significam “BOT OPERA” nem “bom para entrar”.
  Última rejeição informa escopo e horário, sem fingir autoridade do TF atual.
- Níveis, análise expansível e indicadores; gráfico/análise empilhados abaixo
  de 1280px, evitando o painel comprimido. Mesmos widgets e ações.
- Respostas antigas de análise/Multi-TF e callbacks de preço encerrados não
  substituem o ativo atual. API não cancela I/O: a UI descarta resposta obsoleta.
- Registro de plano e confirmação de posição existente continuam ações
  distintas. Rotas, payloads, confirmação e permissões preservados.

## Verificação e limites

10 testes novos de diagnóstico; regressão manual de 108 testes aprovada antes
da ampliação final dos testes (também verificada isoladamente). PG16 local
descartável com rede bloqueada: boot 46, dispatch 32 e fechamento 73 checks,
todos aprovados. Frontend: 115 testes, TypeScript e build temporário aprovados.
`frontend/dist` preservado. Estado final: 3.323 testes backend executados,
3.321 aprovados e dois skips históricos R05C (fixture privada indisponível),
em 648,9s. Os 299 críticos e 113 de apresentação/diagnóstico também passaram.
Pyramiding/risco em PG descartável: 21 checks adicionais aprovados.

QA responsiva em 390/768/1024/1440px: largura do documento igual à viewport,
com análise empilhada nas menores e lateral no desktop amplo. Capturas em
390/1024/1440; troca SYN→SYN2 conferida pelo DOM (TP1 ultrapassado→na zona).
Somente fixtures sintéticas e ações de leitura; isolamento bloqueia transporte
externo e mutações. Sem teste em aparelho físico ou leitor de tela real.
Seis asserts antigos de markup R06A/B1 foram adaptados ao layout autorizado,
preservando força ≠ probabilidade, valor passado à barra e corte/callback da
ação manual. Compatibilidade de payload legado R06B2 preservada.

O bloco novo não altera score, estratégia, risco, alavancagem ou flags. A
publicação conjunta inclui os seis commits locais anteriores, inclusive as
correções de preflight/risco de pyramiding já autorizadas e seus laboratórios
isolados. Não ativa candidata, pesquisa, coleta ou promoção automaticamente.
MAKER/PYRAMIDING/LIVE_SIZE_MULT existentes não foram alterados.

Reconhecimento pode voltar a invalidar se a posição mudar; não há atomicidade
com uma ação feita diretamente na exchange. Desbloquear esse motivo não garante
entrada ou lucro. Próxima validação operacional: observar uma entrada legítima
se os gates a admitirem, conferindo fill, quantidade, proteção e contabilidade;
nunca fabricar trade para obter aceite. Probabilidade de tocar TP1 não é chance
de o bot entrar nem probabilidade de lucro líquido.
