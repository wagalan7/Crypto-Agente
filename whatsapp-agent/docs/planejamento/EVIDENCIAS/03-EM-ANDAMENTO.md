# E03 — registro intermediário (ENCERRADO)

⚠️ **Este arquivo está obsoleto.** A etapa foi retomada em 13/10/2026 e concluída
localmente. O registro definitivo é **[`03.md`](03.md)**.

Mantido apenas como histórico e para corrigir duas afirmações erradas que ele
continha.

---

## Correções do que este registro dizia

### 1. O procedimento de reversão aqui descrito era PERIGOSO

Dizia para usar `git checkout -- backend/agent.py …` para "voltar ao estado
estável (E02 aprovada)". **Isso apagaria E01, E02 e E03 de uma vez**, porque
nenhuma delas foi commitada: `git checkout` leva ao **HEAD**, que é a build 67.

O procedimento correto está em
[`../homologacao/ROTEIRO_E03.md`](../homologacao/ROTEIRO_E03.md) §4: snapshot
verificável primeiro, só depois qualquer desfazer.

### 2. "Idempotente por valor" não bastava

Este registro afirmava que confirmar, cancelar e remarcar não precisavam de
marca por evento porque gravam um estado e não acumulam. Errado: o risco é
**reaplicar uma decisão antiga depois de uma ação posterior** — um retry
atrasado da confirmação, depois de o paciente ter remarcado, confirma o horário
errado, zera os marcadores de lembrete e redispara aviso.

Corrigido: **todas** as mutações do fluxo de entrada passaram a ter identidade
de evento, com a marca e a mutação na mesma transação, e com a posse do lease
validada dentro dela. Ver `03.md` §2.1 e `tests/test_idempotencia_mutacoes.py`.

---

## Checklist de retomada — concluído

| Item | Situação |
|---|---|
| A. Integração quebrada em `main.py` (`mark_slot_offer_delivered`, envio direto) | ✅ |
| B. Rewire dos webhooks com persistência antes do ACK e dedup na Evolution | ✅ |
| C. Configuração, lifespan e shutdown dos workers, sem thread duplicada | ✅ |
| D. Identidade da oferta amarrada ao evento e à mensagem de saída | ✅ |
| E. Testes de E02 adaptados; teste de oferta não enviada substituído por bloqueio | ✅ |
| F. Consulta e ações de suporte sob MASTER_KEY, com auditoria | ✅ |
| Identidade de evento em TODAS as mutações | ✅ |
| Inventário dos envios diretos e janelas | ✅ |
| Migração desde o schema de E02 em cópia fictícia | ✅ |

Última execução: **298 testes, 0 ignorados, 0 falhas** (13/10/2026).
Estado: **PRONTA LOCALMENTE**, não homologada, não publicada.
