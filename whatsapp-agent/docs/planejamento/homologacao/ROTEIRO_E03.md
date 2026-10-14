# Roteiro de homologação e reversão — E03

Recebimento durável, serialização por contato, outbox e elegibilidade das
ofertas por estado de envio.

Estado: **pronta localmente**. Sem commit, sem push, sem deploy.

---

## 1. Verificar localmente

```bash
cd whatsapp-agent
python3.11 -m venv /tmp/homolog-agente/venv
/tmp/homolog-agente/venv/bin/pip install -r requirements-dev.txt
/tmp/homolog-agente/venv/bin/python -m pytest
```

Esperado: **316 passam, 0 ignorados**. A suíte usa banco temporário, provedor
`mock`, modelo simulado e os workers **desligados** (cada teste controla quando
o processamento acontece).

Para ver o fluxo completo com provedor falso:

```bash
/tmp/homolog-agente/venv/bin/python -m pytest tests/test_fluxo_ponta_a_ponta.py -v
```

## 2. Configuração do worker

| Variável | Padrão | Observação |
|---|---|---|
| `INBOX_WORKER_ENABLED` | `1` | processa as mensagens recebidas |
| `OUTBOX_WORKER_ENABLED` | `1` | envia as respostas |
| `INBOX_WORKER_INTERVAL_SEC` | 10 | laço de recuperação |
| `INBOX_LEASE_SEC` | 180 | maior que o pior caso do LLM |
| `INBOX_MAX_ATTEMPTS` | 5 | depois: `failed`, visível em `/admin/fila` |
| `OUTBOX_BATCH` | 10 | vazão por ciclo |
| `OFFER_TTL_MIN` | 180 | validade da oferta de horários |
| `INBOX_NUDGE_ON_WEBHOOK` | `1` | processa já na chegada, sem esperar o laço |

⚠️ **`INBOX_WORKER_ENABLED=1` com `OUTBOX_WORKER_ENABLED=0` derruba o boot de
propósito**: as respostas ficariam acumuladas e ninguém as enviaria.

Nenhuma dessas variáveis precisa ser criada para o deploy — todas têm padrão
seguro. Criar só se quiser mudar o comportamento.

## 3. O que observar em homologação

Depende de WhatsApp de teste (bloqueio **B3**). Até lá, **NÃO VERIFICADO**.

| # | O que observar | Onde |
|---|---|---|
| 1 | Fila vazia em regime normal | `GET /admin/fila` (resumo) |
| 2 | Algum envio em `uncertain` | idem — **nunca é reenviado sozinho** |
| 3 | Eventos `failed` acumulando | idem — indica causa sistemática |
| 4 | Eventos sem id do provedor | campo `id_do_provedor: false` |
| 5 | `[inbox] posse perdida` no log | lease curto demais para o LLM |
| 6 | `[outbox] envio N INCERTO` | dimensiona a janela de entrega |
| 7 | Paciente precisando repetir horário | oferta não ficou elegível |
| 8 | Latência da primeira resposta | o empurrão no webhook está ativo? |
| 9 | Pendências `bloqueado:*` | mensagem chegou sem id do provedor: **nada foi agendado** e o paciente foi convidado a repetir. Se aparecer com frequência, o payload do provedor mudou |
| 10 | Pendências `calendar:*` | a consulta local existe, o evento no calendário externo é incerto: conferir a agenda do Google/iCloud |

### Como agir em cada pendência

```bash
# ver a fila (precisa da MASTER_KEY)
curl -s "$BASE/admin/fila?key=$MASTER_KEY" | python3 -m json.tool

# fechar uma pendência depois de conferir, com motivo registrado
curl -s -X POST "$BASE/admin/fila/pendencia/<id>/resolver?key=$MASTER_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"motivo":"confirmado por telefone com a paciente"}'
```

Resolver **não executa** a ação bloqueada: quem confere a agenda ou fala com o
paciente é a pessoa. Resolver é dizer "já tratei", com rastro em `audit_log`.

## 4. Reversão — leia antes de executar

🔴 **NÃO use `git checkout -- <arquivos>`.** E01, E02 e E03 **não foram
commitadas**: `git checkout` volta ao **HEAD (build 67)** e apaga as três.
O registro intermediário sugeria isso e estava errado.

### 4.1 Snapshot verificável primeiro

```bash
cd "<raiz do worktree>"
DEST=~/snapshot-agente-$(date +%Y%m%d-%H%M)
mkdir -p "$DEST"

# 1. alterações em arquivos rastreados
git diff > "$DEST/alterado.diff"

# 2. arquivos novos (não rastreados), preservando caminhos
git ls-files --others --exclude-standard -- whatsapp-agent .github \
  | tar -czf "$DEST/novos.tar.gz" -T -

# 3. conferência
git diff --stat | tail -1
tar -tzf "$DEST/novos.tar.gz" | wc -l
```

Confirme que `novos.tar.gz` contém `whatsapp-agent/tests/`,
`whatsapp-agent/docs/planejamento/`, `whatsapp-agent/backend/inbox.py`,
`backend/outbox.py`, `whatsapp-agent/pytest.ini`,
`whatsapp-agent/requirements-dev.txt`, `whatsapp-agent/CLAUDE.md` e
`.github/workflows/`. **Só depois** considere desfazer qualquer coisa.

### 4.2 Reverter só a E03, mantendo E01/E02

Não há como fazer isso por git, porque não há commits separando as etapas. O
caminho é manual e deve ser decidido com o diff em mãos:

- remover `backend/inbox.py`, `backend/outbox.py`;
- desfazer, em `backend/main.py`, os blocos marcados `E03`;
- desfazer, em `backend/database.py`, as tabelas/funções de E03 — **sem apagar
  dados**;
- restaurar as funções `save_slot_offer`/`get_slot_offer` de E02;
- remover `tests/test_recebimento_duravel.py`, `test_outbox_ofertas.py`,
  `test_idempotencia_mutacoes.py`, `test_fluxo_ponta_a_ponta.py` e reverter as
  adaptações nos testes de oferta.

**Recomendação:** não reverter. Commitar E01→E03 juntas num branch, de onde o
git passa a poder desfazer de verdade.

### 4.3 Reversão de dados — não é segura só porque o DDL é aditivo

Antes de reverter uma versão **já publicada**:

| Resíduo | Consequência de reverter | Antes de reverter |
|---|---|---|
| `inbound_events` em `pending`/`processing` | o código antigo não os conhece: essas mensagens **ficam sem resposta** | drenar a fila ou listar o que ficou |
| `outbox` em `pending`/`uncertain` | respostas prontas que ninguém envia | drenar; resolver os incertos |
| `offers` em `active` | o antigo lê `slot_offers`, que está vazia: todo "quero o 1" pede esclarecimento até o paciente repetir o horário | degradação aceitável; avisar o cliente |
| `applied_effects` | inofensiva se ignorada | — |

🔴 **Nenhuma tabela é removida por esta etapa, e `slot_offers` não deve ser
apagada por suposição de que está vazia.** A verificação é um
`SELECT COUNT(*) FROM slot_offers` no banco de produção, feita por quem tem
acesso. Só com o resultado em mãos é que se decide.

### 4.4 Backup e teste de restauração em isolamento

```bash
# cópia do banco de produção para um diretório temporário (feita por quem tem acesso)
cp /data/consultorio.db /tmp/restauracao/consultorio.db
export DATA_DIR=/tmp/restauracao
/tmp/homolog-agente/venv/bin/python -c \
  "import sys; sys.path.insert(0,'backend'); import database as d; d.init_db(); \
   print('migrou')"
```

`tests/test_fluxo_ponta_a_ponta.py::test_migracao_desde_o_schema_de_e02` já faz
isso com um banco fictício no schema de E02 e confirma que nada é removido.

## 5. Reinício

Reiniciar o processo é seguro por desenho:

- o evento em processamento tem `lease_until`; quando vence, outro worker o
  retoma e o antigo não consegue mais mexer;
- o estágio gravado diz onde parou: a decisão não é recalculada e a ação não é
  reaplicada;
- envios em `sending` voltam a `pending` pelo mesmo mecanismo de lease;
- envios em `uncertain` **não** são reenviados: ficam para resolução humana.

Depois do reinício, conferir `GET /admin/fila`: fila vazia em regime normal.
