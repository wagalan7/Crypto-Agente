# Feeds FICTÍCIOS para a prévia do boletim (E08B)

Usados só com `NOTICIAS_FIXTURES=<esta pasta>`, que troca as fontes reais por
fontes fictícias no host reservado `noticias-ficticias.invalid` (nunca
resolve). O painel mostra a faixa "PRÉVIA — notícias fictícias" e não cria
links. O modo é recusado se o banco estiver em `/data` (produção).

Nada aqui foi publicado por nenhuma fonte. As datas são relativas
(`{{RFC822:-1d}}`, `{{ISO:-30h}}`) para que a prévia sempre tenha itens da
semana.
