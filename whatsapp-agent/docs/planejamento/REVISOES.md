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
