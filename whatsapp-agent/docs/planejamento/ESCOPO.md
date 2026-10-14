# Escopo e governança — versão 1.0, 12/10/2026

## Resultado contratado para o planejamento
Um único Agente Consultório, com módulos especializados e interface integrada. Evolução incremental do código atual; nenhuma implementação do produto foi executada ao criar este plano. Baseline inicial de 24 semanas: 12/10/2026 a 28/03/2027. Datas são metas de planejamento, não estimativas confirmadas por inspeção do código.

## Premissas
Um responsável técnico usando Claude Code, executando uma etapa por vez, com disponibilidade regular nos dias úteis; proprietário revisa entregas e fornece contas/dados fictícios/modelos em tempo hábil. Não há equipe ou capacidade informada, portanto essa premissa será validada na etapa 01. Produção atual deve continuar operando; homologação, banco e número de teste separados. Sem expectativa de trabalho obrigatório em fins de semana ou feriados. Semanas de Natal/Ano-Novo reservadas a revisão, validação e recuperação de atrasos. Recursos dependentes de credenciais, aprovação de plataforma ou orientação profissional podem bloquear publicação.

## Escopo funcional fechado
| ID | Módulo | Incluído e critério de resultado |
|---|---|---|
| S01 | Base confiável | Entrada durável, retomada, dedup por tenant/provedor/evento, ordem por contato, envio incerto visível, monitor externo, alertas e runbook; falhas injetadas não desaparecem silenciosamente |
| S02 | Agenda correta | Criar, remarcar, cancelar, confirmar, comparecimento, duração, modalidade, bloqueios, recurso/profissional e fusos; concorrência validada e resposta canônica; não transformar criação em remarcação sem intenção válida |
| S03 | Segurança e privacidade | Papéis, sessões revogáveis, isolamento, webhooks autenticados, cifra e custódia de chave, logs mínimos, backup restaurável, direitos e política de guarda por categoria; biblioteca e clínico segregados |
| S04 | Identidade e UX | Nome do estabelecimento, profissional e saudação separados; navegação responsiva, componentes consistentes, acessibilidade, estados vazios/erro/carregamento e design aprovado |
| S05 | Hoje e pacientes | Pendências com definição e lista filtrada, agenda do dia, nomes de contatos conhecidos, cadastro, busca/paginação, ativo/inativo e ficha individual; contagens batem com resultados |
| S06 | Agenda visual | Grade diária/semanal, lista móvel, espaços livres, bloqueios e online/presencial; recorrência de disponibilidade e ajustes pontuais separados; navegação sem exigir arrastar |
| S07 | Atendimento | Caixa de entrada responsiva, envio pelo sistema, entrega/falha, pausa/retomada, recados atribuídos, FAQ aprovado, áudio com transcrição revisável e fallback para mídia; não interpreta sentimentos nem contexto clínico |
| S08 | Financeiro | Cobranças por sessões, avulsas, vencimentos, pagamentos/estornos registrados, comprovantes em revisão, conciliação, despesas, recorrências administrativas, fluxo de caixa, totais e exportações; valores monetários exatos e autorização |
| S09 | Assinatura do SaaS | Preservar Stripe/MP e onboarding, assinaturas verificadas, replay, vínculo ao tenant/valor/plano, suspensão/reativação e medição de consumo; sandbox antes de oferta |
| S10 | Contratos e documentos | Contratos atuais organizados, modelos versionados, busca/filtros/paginação; declaração de comparecimento com horário como primeiro modelo, rascunho/revisão/emissão, PDF, histórico e vínculo ao paciente; não prometer assinatura qualificada inexistente |
| S11 | Assistente central | Bolinha acessível em desktop/celular; texto e voz opcional com fallback; consulta dados canônicos, abre listas filtradas, prepara documentos, agenda/tarefas/cobranças; ferramentas autorizadas, confirmação por risco, custo e limites |
| S12 | Secretaria do profissional | Tarefas com responsável/prazo/estado, lembretes, resumo diário configurável, agenda e pendências; comandos por identidade autenticada, nunca apenas fromMe |
| S13 | Registros profissionais | Espaço por paciente, autoria, datas, versões/adendos, anexos, acesso auditado, exportação autorizada e regras de guarda; não converter chat em prontuário automaticamente nem permitir apagamento irrestrito |
| S14 | Biblioteca teórica | Referências, conceitos, notas, trechos e materiais autorizados; autor/obra/página/versão, upload limitado e indexação, busca por profissional/permissão, remoção/atualização propagadas |
| S15 | Pontes teóricas | Busca em ambos os sentidos por opção explícita, trechos exatos de registro e fonte, tema relacionado e decisão vincular/ignorar; nenhum diagnóstico, interpretação, conclusão ou edição automática de registro |
| S16 | Lousa | Cartões, notas, ligações, referências, salvar/duplicar, exportar; compartilhamento de cópia selecionada, revogável e com expiração, sem exposição implícita de prontuário |
| S17 | Simulador | Custos, tributos informados, remuneração desejada, capacidade/ocupação, duração e cenários; cálculos visíveis e hipóteses editáveis; não aconselhamento fiscal nem preço clínico obrigatório |
| S18 | Relacionamento | Lista de espera/encaixe com disputa atômica, preferências/opt-out, retorno e reativação preparados e aprovados; sem campanhas autônomas irrestritas |
| S19 | Equipe e integrações | Vários profissionais com agendas e permissões, Google/CalDAV reconciliados, opção oficial WhatsApp com políticas específicas; consumo e quotas por tenant; avaliar capacidade com testes antes de definir limite comercial |
| S20 | Comercial e site | Revitalizar landing existente, marca/textos, demonstração fictícia, oferta por recursos publicados, onboarding assistido, suporte, preços configuráveis com custos e limites; validar checkout e conversão básica |

## Arquitetura alvo
Interface única e coordenador de ferramentas. Especialistas lógicos: Atendimento, Secretaria, Financeiro, Documentos, Biblioteca. Não é obrigatório criar processos independentes nem usar LLM onde cálculo/consulta determinística basta. Coordenador recebe identidade derivada da sessão, resolve permissão e ferramenta, limita chamadas e compõe resposta. Especialistas não têm SQL livre nem acesso global; biblioteca/registros jamais entram no contexto de atendimento público.

Fonte canônica compartilhada por assistente, cards, páginas e relatórios. Entidades previstas: tenant, usuário/papel, profissional/recurso, paciente, appointment, message_event/outbox, conversa/recado, tarefa, cobrança/recebimento/despesa, documento/modelo, registro profissional, referência/trecho/vínculo, lousa/compartilhamento e eventos de auditoria. Reutilizar entidades existentes, nomes finais definidos no código. Campos monetários em centavos/decimal adequado; instantes e fusos explícitos. Estados de registros cancelados e financeiros não podem ser apagados para corrigir inconsistência.

Preservar Python/FastAPI e frontend atual inicialmente; reorganização incremental. SQLite permanece até testes de volume e coordenação evidenciarem necessidade de mudança. Preparar adaptadores e coordenação de jobs/filas antes de habilitar mais réplicas. Migração para outro banco só por mudança aprovada de escopo. Plataforma de sites separada e novo framework frontend não são requisitos; revitalizar site existente.

## Fora desta versão
Diagnóstico, interpretação clínica automatizada, recomendação de tratamento, testes psicológicos automatizados, treinamento de modelo com prontuários, gravação/transcrição integral de sessão terapêutica, teleconsulta, prescrição, convênios/TISS, emissão fiscal, movimentação bancária automática, contabilidade oficial, apps nativos, marketplace e expansão para 13 segmentos com fluxos próprios. Documentos clínicos complexos não serão redigidos autonomamente. Importação genérica de qualquer sistema legado não está incluída; migrações do banco atual estão.

Não oferecer contratos juridicamente certificados, disponibilidade garantida, redução de faltas ou economia percentual sem validação. Materiais de terceiros exigem direito de uso; não contornar acesso de livros/cursos. Orientações profissionais para documentos e guarda devem ser validadas antes da ativação correspondente: [Manual CFP](https://site.cfp.org.br/publicacao/manual-orientativo-de-registro-e-elaboracao-de-documentos-psicologicos/).

## Critérios comuns a toda entrega
1. Dados fictícios e homologação isolada; autorização no servidor e testes de tenant/papel nos novos acessos.
2. Critérios específicos reproduzidos, testes relevantes executados e CI verde para revisão exata; não testes que apenas repetem implementação.
3. Migração segura e rollback/restauração testados quando houver mudança persistente. Não destruir registros reais, sessões ou chaves.
4. Interface utilizável no celular/desktop, sem dados técnicos desnecessários, ações claras e estados de falha.
5. Evidência de comportamento ponta a ponta para recurso ofertado; dependência não exercitada rotulada NÃO VERIFICADA.
6. Checklist de privacidade, operação, treinamento e disponibilidade de suporte proporcional ao módulo.
7. Recurso pronto localmente não significa publicado. Homologação e publicação são estados separados.

## Portões de venda
R0: demonstração e lista de interessados podem começar em 12/10 com dados fictícios. Clientes atuais recebem acompanhamento; isso não autoriza expandir riscos.
R1: piloto de atendimento/agendamento somente depois de E02–E06, avaliação de proteção dos dados atuais, restauração e regressões essenciais aprovadas. A meta é fim da semana 04; se falhar, venda continua apenas como demonstração/lista de espera. Máximo inicial sugerido: dois novos consultórios.
R2: experiência renovada e atendimento validado após E07–E12.
R3: financeiro/documentos após E13–E16; oferta limitada aos modelos e integrações publicados.
R4: assistente/secretaria após E17–E18, ou liberar consultas de leitura antes se seus critérios estiverem aprovados.
R5: registros/biblioteca após E19–E21; E22 só após avaliação de fontes, permissões e influência indevida.
R6: lousa/simulador/relacionamento após E23–E25.
R7: equipe e opção oficial após E26; lançamento consolidado após E27–E28.
Não esperar tudo para vender. Também não vender a promessa completa como disponível antes desses portões.

## Controle de escopo e calendário
Baseline v1.0 é o acordo inicial. Executar por ordem/dependência, sem adicionar desejos ao prompt ativo. Novas ideias entram em backlog fora da versão. Mudança exige registro: motivo, requisito afetado, trabalho, risco, datas e aprovação do proprietário. Não reduzir proteção para manter data.
Na etapa 01, medir condição do código e capacidade do executor; confirmar baseline ou propor v1.1 para aprovação, sem replanejamento automático. Revisão semanal curta: entregue, evidência, bloqueios, próximo marco e impacto nas datas. Se houver atraso, registrar imediatamente; cumprir critério antes de avançar. As estimativas após E01 continuam sujeitas a dependências externas.

## Evidência e fontes
Planejamento derivado da auditoria de 13 arquivos, comentários do proprietário e estratégia consolidada nesta conversa. Código e produção ainda não foram inspecionados neste planejamento. A auditoria mistura leitura estática com alguns testes e observações; Claude deve conferir cada achado. Índice único não basta para intervalos sobrepostos: [SQLite transações](https://www.sqlite.org/lang_transaction.html). Evolução de imagem requer digest testado e análise de ativação: [Evolution 2.4.0-rc1](https://github.com/evolution-foundation/evolution-api/releases/tag/2.4.0-rc1). Base legal/retencão não deve ser resolvida por checkbox genérico: [ANPD](https://www.gov.br/anpd/pt-br/acesso-a-informacao/perguntas-frequentes).
