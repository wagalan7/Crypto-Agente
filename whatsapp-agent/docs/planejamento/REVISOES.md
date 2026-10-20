# Revisões do plano

A baseline **v1.0** (ESCOPO.md e CRONOGRAMA.md) é preservada como acordo
inicial e não é editada. Toda alteração aprovada entra aqui, com origem, motivo,
requisitos transferidos e efeito nas datas.

---

## v1.1 — 12/10/2026 · antecipação de conteúdo para E02

**Aprovada por:** proprietário, no prompt de execução da E02 revisada.
**Datas:** mantidas. Nenhuma semana do CRONOGRAMA foi alterada.
**Escopo total:** inalterado. Trabalho foi **movido entre etapas**, não adicionado.

### Requisitos transferidos

| # | Origem | Destino | Requisito |
|---|---|---|---|
| 1 | E04 (semana 3) | **E02** (semana 2) | Correção da janela e da seleção de horários: separar horizonte por dias do limite de quantidade; consultar a data/período pedidos; não usar índice global nem primeiro horário livre para executar ação; exigir slot compatível com o pedido; confirmação canônica só depois da gravação |
| 2 | E27 (semana 23) | **E02** (semana 2) | Retirada de depoimentos e métricas sem evidência do material comercial |

### Motivo

**Requisito 1.** E01 reproduziu em execução local o relato "pedi segunda-feira e
fui confirmado para quarta" (`EVIDENCIAS/01.md` §3). É defeito ativo em
produção, grava o paciente em dia que ele não escolheu e **se apresenta como
confirmação correta** — a reescrita de texto anti-alucinação substituía o dia
dito pelo modelo pelo dia escolhido pelo backend. Manter isso em aberto até a
semana 3 significaria mais duas semanas de agendamento errado silencioso num
cliente real.

**Requisito 2.** E01 encontrou na landing seis depoimentos atribuídos a pessoas
inexistentes e métricas nunca apuradas — "40% menos faltas no primeiro mês", "2
horas livres por dia", "em 10 minutos estava tudo pronto" — além de "Mais de 30
psicólogas já usam" com 8 consultórios cadastrados e 1 em operação. O próprio
ESCOPO v1.0 proíbe: *"Não oferecer ... redução de faltas ou economia percentual
sem validação"*. Era conflito do plano consigo mesmo, e exposição de publicidade
enganosa enquanto o site estiver no ar.

### O que **não** foi transferido

**E04 continua responsável, na semana 3, por:** atomicidade de verificação e
escrita (índice único e transação), concorrência com duas conexões independentes,
intervalos e duração, estados de consulta, recurso/profissional, modalidade,
autorização nas entradas de criar/remarcar, criação convertida em remarcação sem
intenção válida, horário contíguo e reutilização de canceladas, e transição de
horário de verão. E02 tratou **um** caminho (seleção de horário no atendimento
por WhatsApp), não a agenda inteira.

**E27 continua responsável, na semana 23, por:** revitalização completa da
landing com design aprovado, demonstração dos recursos publicados, comparação de
planos por capacidade medida, onboarding assistido, SEO, acessibilidade e
desempenho. E02 apenas **retirou alegação sem prova**; não redesenhou nada.

Nenhuma das duas etapas é marcada como concluída.

### Capacidade de execução (resposta ao bloqueio B1)

O proprietário informou **disponibilidade diária para execução e revisão**. Isso
atende a premissa do ESCOPO v1.0 ("um responsável técnico ... com disponibilidade
regular nos dias úteis") e **confirma a baseline de 24 semanas**, sem alteração
de datas. B1 fica encerrado; B3 (contas externas de homologação) segue aberto e
continua sendo o maior risco de calendário.

---

## v1.2 — 19/10/2026 · direção visual definida

**Aprovada por:** proprietário, em `identidade-visual/DIRETRIZ-MARCA.md`.
**Datas:** mantidas. Nenhuma semana do CRONOGRAMA foi alterada.
**Escopo total:** inalterado. É **insumo** de etapas existentes, não requisito
novo; nenhum trabalho foi movido entre etapas.
**Estados:** nenhum alterado. E08 e E27 continuam PENDENTES.

### O que mudou

A identidade visual do app, dos próximos módulos e da página de vendas passa a
ser vinho / rosé / nude / off-white, no lugar do violeta atual (`brand`
`#7c3aed` em cinco templates e nos protótipos anteriores não versionados). A
estrutura leve dessas referências é preservada: espaços, cards, filtros, agenda
em grade e painéis de detalhes. Logo principal: `04-logo-fornecida.png`.

### Onde está

| Arquivo | Conteúdo |
|---|---|
| `identidade-visual/DIRETRIZ-MARCA.md` | diretriz original, cópia inalterada |
| `identidade-visual/referencias/01–04` | as quatro imagens, cópias byte a byte (sha256 conferido) |
| `identidade-visual/REGISTRO.md` | inventário, papel de cada imagem, medições, restrições |
| `ETAPAS/08.md`, `ETAPAS/27.md` | seção "Revisão v1.2" |
| `prompts/08.txt`, `prompts/27.txt` | nota v1.2 apontando para os itens acima |

### Medido no registro, não presumido

- A logo é RGB **sem transparência**, com fundo preto sólido; a assinatura tem
  **1,14:1** contra esse fundo. Precisa de preparação antes de uso (E08).
- As amostras de paleta dos painéis 01 e 03 **divergem** em todas as famílias:
  os HEX registrados são aproximações, não valores oficiais.
- O rosé fica no limite de AA para texto normal (4,51–4,76:1); nude, rosa suave
  e off-white só servem como superfície ou decoração.

### Aplicação

E08 consolida tokens, ativos e componentes depois de protótipo com dados
fictícios (Hoje, Agenda, Financeiro; desktop e celular). Os módulos seguintes
herdam o padrão. E27 aplica à página de vendas. Uma aplicação visual anterior a
E27 pode ser release separado e revisável, sem alterar o R1 nem misturar com
backend ou filas.

---

## v1.3 — 19/10/2026 · E08A antecipada

**Aprovada por:** proprietário, no prompt de execução da E08A ("alterar o layout
antes de seguir as próximas etapas funcionais").
**Datas da baseline:** **não alteradas.** A ordem nova e o impacto estimado
estão registrados abaixo.
**Escopo total:** inalterado. Parte do trabalho de E08 foi **antecipada**; nada
foi acrescentado.

### O que foi antecipado

E08A = aplicação da identidade visual (v1.2) às telas que **já existem**:
navegação, Início, Agenda e horários, Conversas, Contratos, Cobranças e
Configurações, mais a página de controle acessada a partir do painel.
Evidências em `EVIDENCIAS/08A.md`.

### O que continua em E08 (semana 06) — E08 **não** está concluída

- telas e navegação que dependem de usuários e papéis (E07);
- padrão visual das telas que ainda não existem — Pacientes, Atendimento,
  Financeiro completo, Documentos, Biblioteca — em conjunto com as etapas que as
  criam (E09, E11, E13, E15, E19);
- ativos definitivos de logo (transparente, assinatura para fundo escuro), que
  dependem de arquivo do proprietário;
- iconografia dentro do conteúdo (hoje emoji) e as telas restantes (admin,
  onboarding, pagamento, termos, privacidade);
- aprovação visual registrada como aceite de E08.

E08A **não** conclui E10 (grade de agenda), E13 (financeiro) nem E19, e não
autoriza ofertar recurso futuro.

### Ordem nova e impacto estimado

| | Baseline | Agora |
|---|---|---|
| Semana 02 (19–25/10) | E02, E03 | E02 e E03 já prontas localmente (adiantadas na semana 01) **+ E08A** |
| Semana 03 (26/10–01/11) | E04, E05 | E04, E05 — **sem mudança, se** a avaliação visual e os ajustes couberem na semana 02 |
| Semana 06 (16–22/11) | E08, E09 | E08 menor (parte entregue em E08A), E09 |

**Impacto estimado:** nenhum na baseline enquanto a avaliação visual e eventuais
ajustes terminarem até 25/10. Se passarem disso, E04 atrasa na mesma medida —
registrar aqui, sem mover datas em silêncio. Um release visual separado, se
autorizado, entra como trabalho de publicação, não como etapa.

---

## v1.4 — 19/10/2026 · E27A antecipada (só visual da página de vendas)

**Aprovada por:** proprietário, depois de ver o R4 no ar ("painel ficou ok,
porém a página de vendas não atualizou"), escolhendo antecipar agora.
**Datas da baseline:** **não alteradas.**
**Escopo total:** inalterado. Parte **visual** de E27 foi antecipada; nada foi
acrescentado.

### O que foi antecipado

E27A = aplicação da identidade (v1.2) e dos tokens da E08A às páginas de vendas
que **já existem** — `landing.html` (`/`) e `landing_vertical.html` (`/p/…`):
paleta, tipografia, botões, herói, favicon e imagem de compartilhamento.
Publicação, se autorizada, como release separado **R5**.

### Limites

- **sem mudança de texto, oferta, preço ou desconto**; nenhuma alegação nova e
  nenhuma das retiradas no R1/R1b de volta (a arte antiga do herói contém
  "reduzem faltas" e sai junto com a marca antiga);
- nada do moodboard como prova (`REGISTRO.md`);
- sem backend, banco, rotas ou variáveis.

### O que continua em E27 (semana 23) — E27 **não** está concluída

Textos e mapeamento promessa → evidência (inclusive "2min Para configurar" e os
valores de mercado citados), demonstração dos recursos publicados, comparação
de planos por capacidade medida, preço configurável, onboarding assistido,
SEO/metadados completos, acessibilidade e desempenho medidos, prova social real.

### Impacto estimado

Nenhum na baseline: trabalho de publicação, não etapa. Concorre com a avaliação
visual da E08A na semana 02; se empurrar E04 para depois de 25/10, registrar
aqui.

---

## v1.5 — 20/10/2026 · E08B: boletim de notícias por segmento (requisito novo)

**Aprovada por:** proprietário, complemento autorizado em 19/10/2026 e enviado
para execução em 20/10 (`prompts/08B.txt`), "depois de estabilizar E08A".
**Datas da baseline:** **não alteradas.** Impacto estimado registrado abaixo.
**Escopo total:** **acrescido.** Diferente da v1.3 e da v1.4, que só anteciparam
trabalho já previsto, isto é requisito que não estava no ESCOPO v1.0.

### O que entra

No Início do painel, o quadro narrativo administrativo com IA — que repetia em
parágrafo os cards do resumo — é substituído por **"Psicologia em foco"**: 3 a
5 destaques dos últimos 7 dias de fontes públicas verificadas (CFP; Ministério
da Saúde filtrado por saúde mental), com fonte, data de publicação, resumo da
própria fonte e link. Um boletim por segmento, cache no servidor, atualização
diária às 09:00 de Brasília, com recuperação, lease e pendência operacional.
Só psicologia habilitada; o cadastro de fontes por segmento fica pronto para
expansão.

### O que não entra

- notícias dos outros 12 segmentos (cada fonte exige verificação própria);
- CRPs e fontes de pesquisa;
- resumo por IA (o prompt permite como opção futura, com cache e limite);
- envio por WhatsApp ou e-mail;
- data de evento (os feeds não a informam).

### Esforço e impacto

| | |
|---|---|
| Executado | ~1 dia de trabalho local: implementação, 83 testes, mutações, prévia e verificação real das fontes |
| Falta | avaliação do proprietário; integração com o branch do R2 e a suíte dele; confirmar a saída HTTPS da Railway para as fontes; release próprio |
| Semana 02 (19–25/10) | agora contém E08A (publicada), E27A (publicada) e E08B (pronta localmente) |

**Impacto estimado:** E04 (semana 03, a partir de 26/10) só não atrasa se a
avaliação da E08B e eventuais ajustes couberem até 25/10. Se passarem disso,
E04 atrasa na mesma medida — registrar aqui, sem mover datas em silêncio.
Custo operacional: 2 requisições por dia por segmento, mais no máximo 1 revisão
manual por hora; nenhuma chamada paga. Para psicologia, **deixa de existir** a
chamada de LLM por abertura do Início que a narrativa fazia quando a flag estava
ligada.
