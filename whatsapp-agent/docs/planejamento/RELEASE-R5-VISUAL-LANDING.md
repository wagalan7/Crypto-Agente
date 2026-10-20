# R5 — identidade visual nas páginas de venda (E27A)

Preparado em 20/10/2026. **NÃO PUBLICADO.** Aguarda a avaliação visual do
proprietário e, depois, autorização explícita para o push.

## Versão exata

| | |
|---|---|
| Commit | `78a1718dc18370d2e44004ceb07da8ce286ab2cf` |
| Branch local | `release/r5-visual-landing` (worktree `scratchpad/r5`) |
| Base | `1986bdef0` = `origin/main` (R1b no ar); **fast-forward**, 1 commit |
| Conteúdo | 4 arquivos em `whatsapp-agent/backend/`: `templates/landing.html`, `templates/landing_vertical.html`, `static/marca/compartilhamento-1200x630.jpg` (novo), `static/hero-agente.jpg` (removido) — 268+/163− |
| Fora do release | documentação (fica no branch do planejamento), backend, rotas, banco, variáveis, `landing_content.py`, `_gtag.html` |
| Carimbo de build | não incluído — mesmo motivo do R1/R3/R4: `/healthz` não expõe versão; a confirmação é o `uptime_s` zerar |

## Evidência

`EVIDENCIAS/27A.md`, com 12 capturas antes/depois, o diff e os scripts.
Resumo:

- texto visível, título e metas **iguais** nas 13 páginas, salvo o celular
  ilustrativo (frases já existentes na página); checagem calibrada;
- alegações do R1/R1b: **LIMPO**;
- 0 violeta computado, 0 vazamento horizontal, 0 erro de JS, 0 resposta ≥ 400;
- contraste de todos os pares novos ≥ 4,5:1;
- títulos sem palavra órfã nas 104 combinações; herói em 2 linhas em todas as
  larguras ≥ 768;
- testes de template do R2 passam com estes arquivos.

## O que o visitante vai notar

| Mudança | Observação |
|---|---|
| cores vinho/rosé no lugar do violeta; títulos em Montserrat | a mesma identidade do painel (R4) |
| símbolo da marca no menu, no rodapé e na aba | o cérebro violeta sai |
| herói com um celular mostrando o agendamento | a arte antiga sai, inclusive a frase "reduzem faltas" que estava **dentro da imagem** |
| prévia de link (WhatsApp, redes) com o símbolo | plataformas que guardaram a prévia antiga podem mostrá-la até buscar a página de novo |
| tablet com herói empilhado; telefone do rodapé visível | correções de layout que já existiam em produção |

Nenhum texto, preço, desconto, link ou formulário muda.

## Riscos

| Risco | Avaliação |
|---|---|
| Quebra de função | baixo: só HTML/CSS de duas páginas públicas; rotas e contextos iguais; render conferido nas 13 páginas |
| **Conversão de anúncios** | a landing é o destino do Google Ads. Mudança visual pode mexer na taxa de conversão, para mais ou para menos. A tag (`_gtag.html`) não muda, então a medição continua comparável antes/depois |
| `/static/hero-agente.jpg` passa a 404 | nenhuma referência no repositório; se alguém tiver o link direto da arte, ele quebra. É intencional: a arte traz uma alegação retirada |
| Novo terceiro | nenhum: Tailwind CDN e Google Fonts já eram usados pela landing |
| Tailwind por CDN | **não muda**: se o CDN falhar, a página aparece sem estilo, como hoje |
| Bot de cripto | não é tocado: só o serviço do Agente (`whatsapp-agent`) reconstrói, como no R1, R1b, R3 e R4 |

## Janela

**Depois das 22h (BRT).** A página é pública e não depende do agente, mas o
deploy reconstrói o serviço inteiro, e a troca de contêiner deixou o
atendimento sem resposta por **≤ 19 s** no R4 e no R1b (medido a cada 5 s).
Sem mudança de banco, sem backup extra.

## Publicação (só com autorização)

```bash
git -C <scratchpad>/r5 push origin release/r5-visual-landing:main
```

Antes do push: reconferir que `origin/main` ainda é `1986bdef0`, que o branch
tem 1 commit e que a árvore está limpa.

## Verificação pós-deploy (~10 min)

1. `/healthz`: `uptime_s` zerado, `db: ok`, `tenants_count: 8`; checagem a cada
   5 s para medir a janela.
2. Deployment registrado **só** no serviço do Agente.
3. `GET /static/marca/compartilhamento-1200x630.jpg`: **200**, sha256 igual ao
   do commit (`3d24a8dac7e029e3…`); `GET /static/hero-agente.jpg`: **404**.
4. **Texto visível de produção = texto da prévia do R5**, nas 13 páginas
   (`texto_igual.py` adaptado para comparar produção com a prévia). Como `/` é
   renderizado, o hash do HTML não serve; o texto, sim.
5. Alegações (`alegacoes_texto.py` contra produção): **LIMPO**.
6. Violeta computado = 0 em produção (`capturas.py` contra a URL de produção,
   só leitura de páginas públicas).
7. **Proprietário** abre `/` e uma página de segmento e confirma visualmente.

## Reversão

```bash
git revert 78a1718dc18370d2e44004ceb07da8ce286ab2cf
git push origin HEAD:main
```

Volta os dois templates e o `hero-agente.jpg`; a imagem de compartilhamento
nova fica sem uso, sem efeito colateral. Nenhum dado a restaurar. Outra janela
de ≤ 19 s, mesma regra de horário. Nunca `reset` nem `push --force`.

## Depois do R5

O branch do R2 altera `landing.html` e `landing_content.py` com as **mesmas**
retiradas de texto que o R1 já publicou, porque o R1 saiu do commit 1 do R2.
No rebase do R2 sobre `main`, `landing.html` pode conflitar. A resolução é
manter a versão de `main` (R1 + R1b + R5), que já contém essas retiradas, e
rodar o `test_landing_alegacoes.py` corrigido, que busca no texto visível.
