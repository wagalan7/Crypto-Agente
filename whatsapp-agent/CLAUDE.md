# Agente Consultório (whatsapp-agent) — instruções do produto

Idioma: **PT-BR**. Este diretório é um produto **separado** do bot de cripto que divide o
mesmo repositório. O `CLAUDE.md` da raiz é do bot de cripto e **não se aplica aqui**.

## Limites

- Trabalhe **somente** dentro de `whatsapp-agent/`. Nunca altere `backend/` da raiz do
  repositório (é o bot de cripto) nem `frontend/`, `docs/` ou `cloudflare-worker/` da raiz.
- Railway CLI está deslogado: validação só por `curl` HTTP. Mudanças de variável de
  ambiente e ações no painel são passos do proprietário.
- Não commitar: `.claude/settings.local.json`, `AGENTS.md`, `backend/static/prototipo-*.html`.

## Plano de execução

O plano v1.0 (24 semanas, 28 etapas) vive em **`whatsapp-agent/docs/planejamento/`**:

| Arquivo | Papel |
|---|---|
| `CONTEXTO.md` | regras de execução de cada etapa |
| `ESCOPO.md` | escopo fechado S01–S20, portões de venda, fora de escopo |
| `CRONOGRAMA.md` | janelas semanais da baseline |
| `ETAPAS/NN.md` | trabalho e aceite de cada etapa |
| `prompts/NN.txt` | mensagem pronta para iniciar cada etapa |
| `STATUS.md` | progresso, estados e bloqueios abertos |
| `EVIDENCIAS/NN.md` | evidências por etapa |
| `homologacao/` | ambiente local isolado (banco temporário, dados fictícios) |

Antes de uma etapa: leia este arquivo, `CONTEXTO.md`, o resumo de `STATUS.md` e **só a
ficha da etapa atual**. Uma etapa ativa por vez; não avance sem aceite.

A auditoria do produto (13 documentos, somente leitura, build `2026-07-16-67`) está em
`whatsapp-agent/docs/auditoria-produto/`. **Confira cada achado no código** — a revisão de
E01 refutou 7 deles.

## Deploy (só com autorização explícita do proprietário)

1. Incrementar o carimbo na **linha 2 do `Dockerfile`** (`# build: AAAA-MM-DD-NN`).
2. Mostrar o que vai subir e **confirmar** antes de publicar.
3. `git push origin HEAD:main` → a Railway constrói automaticamente.
4. Confirmar pelo `/healthz`: `instance_start = ts − uptime_s` deve ser posterior ao push,
   com `db: ok`.

Produção: `https://agente-atendimento-production.up.railway.app`.
A Evolution API fica em **outro projeto Railway** — ver
`docs/planejamento/EVIDENCIAS/01.md` §4.5 antes de mexer nela.

⚠️ `railway.toml` traz `watchPatterns = ["whatsapp-agent/**"]`, que casa também
`whatsapp-agent/docs/**`: commitar documentação pode disparar rebuild. O mesmo arquivo
declara healthcheck em `/health`, rota que **não existe** (o app expõe `/healthz`).
Pendência registrada como B2 em `STATUS.md`.
