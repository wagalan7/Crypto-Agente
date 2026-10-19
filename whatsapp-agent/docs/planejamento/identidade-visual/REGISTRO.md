# Identidade visual — registro de 19/10/2026

**Direção aprovada pelo proprietário.** Vale para o app, os próximos módulos e a
página de vendas. Não altera status de release nem autoriza publicar alterações.

A fonte é [`DIRETRIZ-MARCA.md`](DIRETRIZ-MARCA.md), cópia **inalterada** do
original. Este arquivo registra o inventário, o papel de cada imagem e o que foi
**medido** nas referências. Não substitui a diretriz.

## Inventário — originais preservados

Cópias byte a byte. O hash da origem confere com o da cópia.

| Arquivo | Dimensões | Papel | sha256 |
|---|---|---|---|
| `DIRETRIZ-MARCA.md` | — | diretriz do proprietário | `ccac7f49026e6315a04aa0174e8a05b00d3cfaf9e4340929b2357df36a7d1873` |
| `referencias/04-logo-fornecida.png` | 1536×1024 | **logo principal** | `b7143bd6859bee55ac0f6c181ed3d3c8442c61263467513efdae8fcc2dd3b116` |
| `referencias/01-painel-marca.png` | 1536×1024 | paleta e tipografia — **o símbolo deste painel não é logo** | `3589039df4f2c7ef9d0735a106e2f13928f469d844c4ec65ea46db99ebd4adf6` |
| `referencias/02-aplicacoes.png` | 1280×853 | aplicações (redes, telas, peças) | `76b95db24063e743ea82a69cacd90d59b1d4131cfb1cf96bc8d4da24b97df3c1` |
| `referencias/03-variacoes-marca.png` | 1536×1024 | variações, ícones, monocromáticas, aplicações | `a9fdb7e3f30cc0778160d03b51a4467bf34eba14d58ced07d0977d95b17965ba` |

Origem: `Documents/Codex/2026-10-12/…/outputs/identidade-visual/`, que permanece
intacta.

## Logo principal — o que o arquivo é, medido

O arquivo `04-logo-fornecida.png` traz o símbolo em fitas rosé/vinho com rosto de
assistente e a assinatura "AGENTE CONSULTÓRIO".

| Fato | Valor medido | Consequência |
|---|---|---|
| Canal alfa | **não tem** — RGB | não é transparente |
| Fundo | **preto sólido** (`#000000` nos quatro cantos) | usar o arquivo direto em fundo claro mostra um retângulo preto |
| Cor da assinatura | mediana `#30020A` | — |
| **Contraste da assinatura contra o próprio fundo** | **1,14:1** | **ilegível**: o símbolo aparece, o nome praticamente não |

A WCAG isenta logotipos do requisito de contraste. Mesmo assim, o aceite do
proprietário exige "logo correta e **legível**", e 1,14:1 não atende. Isso
confirma, com número, o alerta da diretriz: não prometer legibilidade da
assinatura escura em fundo escuro.

**Preparação necessária antes de usar** (trabalho de E08, com conferência do
proprietário, sem redesenhar o símbolo):

1. recorte com fundo transparente, conferido nas bordas e no brilho das fitas;
2. versão da assinatura para fundo escuro, porque o painel 03 mostra a assinatura
   clara sobre vinho — essa versão **não existe como arquivo** e precisa de
   aprovação;
3. versões monocromáticas e ícone de app, a partir do símbolo do arquivo 04, e
   não do painel 01.

## Paleta — amostras, **não** valores oficiais

As imagens não trazem códigos HEX. As amostras abaixo foram tiradas do **centro
dos círculos** das áreas "Paleta" (trecho 13×13 px, mediana, no ponto mais
uniforme ao redor do centro; desvio máximo de 1,3 por canal, ou seja, áreas
sólidas, não reflexo nem gradiente).

| Família | Painel 01 | Painel 03 |
|---|---|---|
| vinho profundo / bordô | `#3C0E18` | `#24070C` |
| vinho intermediário | `#8A273E` | `#792735` |
| rosé queimado | `#B25665` | `#C56C72` |
| rosa suave | `#E7BFBF` | `#E5B8B1` (o painel 03 tem 5 círculos) |
| nude | `#F1D9D7` | — |
| off-white | `#FCF8F7` | `#F9F0E9` |

**Os dois painéis divergem em todas as famílias.** Isso confirma que não existe
valor oficial nas referências: são **aproximações**. A escolha final é de E08,
no protótipo.

### Contraste preliminar (WCAG 2.x), amostras do painel 01

| Amostra | sobre branco | sobre off-white | texto branco sobre ela | Uso indicado pelo número |
|---|---|---|---|---|
| `#3C0E18` | 16,56:1 | 15,70:1 | 16,56:1 | texto, navegação, fundos de destaque |
| `#8A273E` | 8,61:1 | 8,16:1 | 8,61:1 | botão principal, seleção, ícones, foco |
| `#B25665` | 4,76:1 | **4,51:1** | 4,76:1 | **no limite** de AA para texto normal: apoio e texto grande; não usar como única cor de foco sem conferir |
| `#E7BFBF` | 1,67:1 | 1,58:1 | 1,67:1 | só decorativo / fundo de apoio |
| `#F1D9D7` | 1,34:1 | 1,27:1 | 1,34:1 | só decorativo / fundo de apoio |
| `#FCF8F7` | 1,05:1 | — | 1,05:1 | superfície |

Limiares: texto normal ≥ 4,5:1; texto grande, controles e indicador de foco
≥ 3:1. É uma conferência **preliminar**; E08 refaz o cálculo com os tokens
escolhidos e com os estados reais (hover, foco, desabilitado, erro).

## Tipografia — proposta, não decisão

As referências divergem: o painel 01 usa Montserrat + **Inter**, e o painel 03
usa Montserrat + **Poppins**. A proposta inicial é Montserrat para títulos e
Inter para interface e textos longos, a consolidar no protótipo. Nunca usar três
famílias ao mesmo tempo. Usar fallback e carregamento controlado, preservando
acentos.

## O que a direção substitui

- **Cor:** o violeta/lilás atual (`brand` `#7c3aed` na configuração do Tailwind
  em `dashboard.html`, `controle.html`, `admin_panel.html`, `landing.html` e
  `landing_vertical.html`) e o dos protótipos anteriores
  (`backend/static/prototipo-*.html`, não versionados).
- **Preserva** dessas referências: a organização leve, os espaços, os cards, os
  filtros, a agenda em grade e os painéis de detalhes.

## Material do moodboard que **não** pode ir para o produto nem para a oferta

As imagens são peças de direção, não prova de funcionalidade nem de resultado.
Não copiar:

- **depoimento** "Mariana R., Psicóloga" (painel 02) — o mesmo tipo de conteúdo
  fabricado que o R1 retirou do site;
- **contagens** "12,4 mil seguidores", "45 posts", "12 agendamentos hoje" e
  semelhantes;
- **nomes** de pacientes da lista e a saudação nominal "Bem-vinda, Bruna" —
  dados de exemplo da peça;
- **resultados** não demonstrados: "pacientes mais satisfeitos", "clínica mais
  fluida", "profissionais mais presentes";
- **recursos** mostrados nos menus que não estão publicados (relatórios,
  documentos, lembretes inteligentes e outros) — recurso pendente não aparece
  como disponível;
- **domínio e perfil** `agenteconsultorio.com.br` e `@agenteconsultorioia` — não
  verificados; não presumir que existem ou que pertencem ao proprietário.

Frases de posicionamento como "Tecnologia a favor da sua clínica" e "Mais tempo
para o que realmente importa" são candidatas a texto e passam pelo mapeamento
promessa → evidência de E27.

## Onde se aplica

| Etapa | Aplicação |
|---|---|
| **E08** | consolida tokens, ativos (logo preparada) e componentes depois de protótipo com dados fictícios em desktop e celular; telas prioritárias: **Hoje, Agenda, Financeiro** |
| E09 em diante | módulos novos herdam tokens e componentes de E08 |
| **E27** | aplica à página de vendas completa |
| antes de E27 | uma aplicação visual pode ir em **release separado e revisável**, sem misturar com backend ou filas e sem alterar o R1 |

Nenhuma etapa teve data ou estado alterado por este registro. Ver
`REVISOES.md` v1.2.

Nota de publicação: `whatsapp-agent/` é o `rootDirectory` do serviço na Railway.
Quando estes arquivos forem publicados junto com outro release, o serviço é
reconstruído (as imagens não entram na imagem Docker, que copia só `backend/`).
