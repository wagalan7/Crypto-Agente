# Homologação isolada — Agente Consultório

Ambiente **local**, separado da produção, para executar e verificar alterações antes de
qualquer publicação. Preparado na etapa E01. Nada aqui toca a produção.

## Princípios

1. **Banco próprio e temporário.** O caminho do banco vem de `DATA_DIR`. A homologação
   aponta para um diretório fora do repositório; o arquivo pode ser apagado e recriado a
   qualquer momento.
2. **Só dados fictícios.** Nomes, telefones, e-mails e valores são inventados. Nenhum
   registro real de paciente, nenhuma conversa real, nenhum dump de produção.
3. **Integrações simuladas.** `WHATSAPP_PROVIDER=mock` (apenas registra em log, não envia).
   Sem chave de IA, sem SMTP, sem Stripe/Mercado Pago, sem S3, sem Google/CalDAV.
4. **Sem API paga.** `GROQ_API_KEY` e `ANTHROPIC_API_KEY` ficam vazias. `agent._call_llm`
   levanta `RuntimeError` quando não há chave — então um teste que esqueça de simular o
   modelo **falha**, em vez de gastar crédito em silêncio.
5. **Nada de segredo em arquivo versionado.** Apenas `env.homologacao.example`, com
   valores de exemplo.

## Dependências

Produção roda **Python 3.11** (`Dockerfile`: `python:3.11-slim`). Use 3.11 também aqui —
o `backend/.venv` que existe no repositório é 3.9 e **não serve**.

```bash
python3.11 -m venv /tmp/homolog-agente/venv
/tmp/homolog-agente/venv/bin/pip install -r whatsapp-agent/backend/requirements.txt
```

## Subir

```bash
export DATA_DIR=/tmp/homolog-agente/dados
mkdir -p "$DATA_DIR"
python docs/planejamento/homologacao/seed_homologacao.py
```

O seed cria o banco, aplica as migrações (`init_db`) e insere um consultório fictício com
pacientes, consultas, horários e bloqueios. Imprime o `slug` e o `dashboard_token` para
abrir o painel.

Para servir a aplicação (opcional, leitura no navegador):

```bash
cd whatsapp-agent/backend && DATA_DIR=/tmp/homolog-agente/dados \
  /tmp/homolog-agente/venv/bin/uvicorn main:app --port 8010
```

`POST /test/{slug}/message` simula uma mensagem de paciente sem WhatsApp real — **mas
chama o modelo**, portanto exige chave e **não deve ser usado** nesta configuração.

## Verificar

```bash
python docs/planejamento/homologacao/repro_agenda_dia_errado.py
```

Reproduz o relato "pedido para segunda-feira, confirmação para quarta-feira" com dados
fictícios e **modelo simulado** (nenhuma chamada de API). Ver `../EVIDENCIAS/01.md`.

## O que esta configuração **não** cobre

| Dependência | Situação |
|---|---|
| Envio/recebimento real de WhatsApp | **NÃO VERIFICÁVEL localmente** — exige servidor Evolution e número de teste |
| Comportamento do modelo (Groq/Anthropic) | **NÃO VERIFICADO** — simulado; avaliar com chave própria e orçamento definido |
| Stripe / Mercado Pago | **NÃO VERIFICADO** — exige credenciais de sandbox |
| Google Calendar / CalDAV | **NÃO VERIFICADO** — exige conta de teste |
| SMTP (alertas por e-mail) | **NÃO VERIFICADO** — exige servidor de e-mail |
| Backup S3/R2/B2 e **restauração** | **NÃO VERIFICADO** — exige bucket de teste |
| Variáveis efetivas da Railway | **NÃO VERIFICADO** — sem acesso ao painel |

Essas sete linhas são a fronteira entre "pronto localmente" e "validado em homologação".
Fechá-las depende de contas e credenciais de teste que ainda não existem — registrado
como dependência em `../EVIDENCIAS/01.md`.
