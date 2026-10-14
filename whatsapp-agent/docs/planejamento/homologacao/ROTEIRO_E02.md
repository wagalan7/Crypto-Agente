# Roteiro de homologação e reversão — E02

Alteração: janela/seleção de horários, oferta persistida por conversa, CI e
retirada de alegação sem prova do material comercial.

Estado: **pronta localmente**. Sem commit, sem push, sem deploy.

---

## 1. Verificar localmente

```bash
cd whatsapp-agent
python3.11 -m venv /tmp/homolog-agente/venv
/tmp/homolog-agente/venv/bin/pip install -r requirements-dev.txt
/tmp/homolog-agente/venv/bin/python -m pytest
```

Esperado: suíte verde. A suíte cria o banco num diretório temporário próprio,
usa provedor `mock` e substitui o modelo por um dublê — não toca produção e não
chama API paga.

Reprodução do defeito de origem, com saída legível:

```bash
export DATA_DIR=/tmp/homolog-agente/dados && mkdir -p "$DATA_DIR"
/tmp/homolog-agente/venv/bin/python docs/planejamento/homologacao/seed_homologacao.py
/tmp/homolog-agente/venv/bin/python docs/planejamento/homologacao/repro_agenda_dia_errado.py
```

Esperado: `H1 CORRIGIDA`, H2 sem gravar nada e oferecendo a segunda-feira pedida,
H3 gravando a segunda-feira pedida.

## 2. O que observar em homologação com WhatsApp real

Esta é a parte que **não** foi verificada e que depende de conta de teste (B3).

| # | O que observar | Por que importa |
|---|---|---|
| 1 | O modelo passa a enviar `offered_ids` ao listar horários? | É o que faz o "quero o 1" funcionar. Sem isso o agente pede para o paciente repetir o horário |
| 2 | Com que frequência o paciente responde só com número | Mede o atrito real da mudança (ver "Risco conhecido") |
| 3 | Pedido de dia distante (ex.: "segunda que vem") é oferecido e agendado | Era o defeito de origem |
| 4 | Pedido de dia sem vaga recebe alternativas e nada é reservado | Comportamento novo |
| 5 | Tamanho do prompt e custo por conversa | O contexto mudou de 24 horários em 2–3 dias para ~20 em até 9 dias |
| 6 | Remarcação pelo painel segue funcionando | O painel não usa este caminho, mas compartilha `has_conflict` |

Sem conta de teste, itens 1 a 5 ficam **NÃO VERIFICADOS**.

## 3. Risco conhecido desta mudança

**O que pode piorar:** antes, um "1" do paciente sempre resultava em algum
agendamento — às vezes no dia errado. Agora, se o modelo não declarar
`offered_ids`, o "1" **não reserva** e o agente pede esclarecimento. Em termos de
experiência isso é uma pergunta a mais; em termos de risco é a troca certa
(perguntar de novo é recuperável, marcar no dia errado não é). Mas é uma mudança
observável pelo cliente e precisa ser acompanhada nos primeiros dias.

**Mitigação já no código:** as alternativas oferecidas pelo atalho determinístico
(data+hora indisponível) também ficam persistidas, então ali o número continua
funcionando sem depender do modelo.

**Se o atrito for alto em produção:** a correção é melhorar a emissão de
`offered_ids` (prompt ou pós-processamento determinístico da lista mostrada),
**não** voltar a resolver por posição global.

## 4. Reversão

A alteração é **aditiva no banco** e não destrói dado algum.

### Antes de qualquer commit (situação atual)

```bash
cd whatsapp-agent
git checkout -- backend/agent.py backend/calendar_service.py backend/database.py \
                backend/main.py backend/landing_content.py \
                backend/templates/landing.html railway.toml
rm -rf tests pytest.ini requirements-dev.txt
rm -f ../.github/workflows/agente-consultorio.yml
```

### Depois de publicada

1. `git revert <commit>` e deploy normal (carimbo de build novo no `Dockerfile`).
2. **A tabela `slot_offers` pode ficar.** O código antigo não a conhece e a
   ignora; nenhuma consulta dele a referencia. Removê-la é opcional e só deve
   ser feito com backup à mão:
   ```sql
   -- opcional, não necessário para reverter
   DROP TABLE IF EXISTS slot_offers;
   ```
3. Nenhuma coluna foi alterada ou removida. Nenhuma consulta existente mudou de
   forma. `get_available_slots` mantém a assinatura e o comportamento
   (corte cronológico), então painel e indicadores não dependem da reversão.
4. O conteúdo da landing volta junto com o revert. **Enquanto não houver deploy,
   o site publicado continua exibindo os depoimentos fabricados** — retirada
   local não é retirada do ar.

### O que NÃO é revertido por `git revert`

- Proteção de branch no GitHub e configuração da Railway, se forem aplicadas
  (ver `PROTECAO_PUBLICACAO.md`) — são painéis externos.

---

## 5. Revisão de ofertas (12/10/2026) — o que mudou e o que observar

### Verificação local

```bash
/tmp/homolog-agente/venv/bin/python -m pytest tests/test_oferta_coerencia.py
```

Esperado: 34 passam. Cobrem ordem invertida, id inválido/ocupado/duplicado,
horário citado e ausente dos ids, "1" com `slot_id` da opção 2, oferta ausente,
vencida, substituída, isolada por contato e consultório, e o limite de 6 opções.

### Mudança visível ao paciente

A mensagem com a lista de horários agora é **escrita pelo servidor**:

```
Claro, Aurora! 😊 Para Segunda, 19/10 de manhã tenho estes horários:
  1. Segunda, 19/10 às 08:30
  2. Segunda, 19/10 às 09:20
  ...

Qual prefere? 😊
```

O modelo deixou de redigir esse trecho — ele apenas escolhe **quais** horários
oferecer (`offered_ids`). Observar em homologação:

| # | O que observar | Por que |
|---|---|---|
| 1 | O tom da mensagem de oferta ficou aceitável? | É o único trecho da conversa que o modelo não escreve mais |
| 2 | O modelo respeita o limite de 5–6 ids? | Acima disso o servidor corta em 6 |
| 3 | Com que frequência o modelo manda `offered_ids` vazio ou inválido | Nesse caso o servidor monta a oferta sozinho, do dia pedido |
| 4 | Aparece no log `modelo mandou slot_id ... mas o paciente escolheu a opção N` | Mede quantas vezes o modelo discordaria da escolha do paciente |
| 5 | Aparece no log `resposta lista horários sem oferta nova` | Modelo listando horários fora de `list_slots` |
| 6 | Aparece no log `resolvendo opção N contra oferta ... não marcada como entregue` | É a janela de entrega; dimensiona o trabalho de E03 |

### Reversão

Sem passo adicional: `version` e `delivered` entram por `ALTER TABLE` com
`DEFAULT` sobre uma tabela que nunca foi publicada. O procedimento do §4
continua valendo integralmente.
