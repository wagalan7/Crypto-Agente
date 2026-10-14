# Como executar e economizar contexto
1. Abra Claude Code no repositório do Agente, não nesta pasta de entregas. Forneça o ZIP deste plano ou copie seu conteúdo para uma pasta acessível ao Claude. Os links locais desta conversa não são automaticamente acessíveis em outro ambiente.
2. Envie somente prompts/01.txt. E01 instala as fontes de verdade em docs/planejamento e confere a realidade. Forneça a auditoria uma única vez para essa etapa. Não enviar toda a conversa.
3. Após revisar o resultado/aceite, envie prompts/02.txt, depois 03, e assim por diante. Use os arquivos individuais: cada um já contém objetivo e aceite e aponta para contexto persistente pequeno.
4. Quando precisar corrigir, use: “Continue E[NN]. Falharam estes critérios: [itens/evidência]. Corrija só eles, execute os testes afetados e atualize STATUS/EVIDENCIAS. Não avance.”
5. Novo chat no Claude: envie o prompt atual; ele lê CONTEXTO, STATUS e a ficha da etapa, sem recuperar conversa antiga. Resumos curtos; detalhes só em evidências consultadas por necessidade.
6. Revisão: “Revise E[NN] contra sua ficha e escopo. Confira alterações/testes. Liste apenas critérios sem evidência ou defeitos relevantes. Não implemente novas funções.”
7. Publicação é separada: Claude prepara versão, migrações, configuração e reversão; proprietário revisa e autoriza o release exato. Não transformar aceite local em autorização automática de deploy.

## Economia responsável
Contexto fixo tem cerca de uma página; prompts individuais têm trabalho e critérios objetivos. Buscas por símbolos antes de leituras grandes; não resumir auditoria repetidamente, imprimir arquivos inteiros ou refazer testes amplos sem razão. Rodar testes afetados e checks obrigatórios; integração/regressão completa nos gates. Não omitir validação para economizar tokens. Tamanho de tokens depende do modelo e tokenizer: o pacote não promete consumo numérico.

## Arquivos
ESCOPO define o resultado; CRONOGRAMA as janelas; CONTEXTO as regras; ETAPAS o trabalho/aceite; STATUS o progresso. prompts/ são mensagens prontas para enviar. Tudo versionado v1.0. Se proprietário mudar escopo, registrar aprovação e atualizar fontes de verdade juntas; não editar apenas o prompt.

## Perguntas de E01
Confirmar executor/capacidade semanal, acessos de homologação, papéis, modalidades e recursos, definição de ativo, regras de financeiro, modelos documentais, responsável pela revisão profissional, governança de materiais e contas externas. Não pedir tudo de novo em cada etapa. Dependências não resolvidas ficam registradas com responsável e data necessária.
