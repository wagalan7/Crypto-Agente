"""Prova de que os testes críticos do E08B não são vazios: cada mutação quebra
de propósito uma garantia do código e o teste correspondente TEM de falhar.
O arquivo original é restaurado ao fim de cada mutação.

Uso: python mutacoes.py <whatsapp-agent> <python-do-venv>
"""
import subprocess
import sys
from pathlib import Path

RAIZ, PY = Path(sys.argv[1]), sys.argv[2]
B = RAIZ / "backend"

MUTACOES = [
    ("janela de 7 dias vira 30", B / "noticias.py",
     "JANELA = timedelta(days=7)", "JANELA = timedelta(days=30)",
     "tests/boletim/test_noticias_selecao.py::test_janela_de_sete_dias_corridos"),
    ("DTD/ENTITY deixa de ser recusado", B / "noticias.py",
     "    if _DTD.search(conteudo):", "    if False and _DTD.search(conteudo):",
     "tests/boletim/test_noticias_coleta.py::test_doctype_ou_entity_recusados"),
    ("redirect deixa de passar pela allowlist", B / "noticias.py",
     "        if not _host_permitido(url, fonte):\n            raise ErroColeta(\"endereço fora da allowlist da fonte\")",
     "        pass",
     "tests/boletim/test_noticias_coleta.py::test_redirect_para_host_fora_da_fonte_e_recusado"),
    ("Atom passa a usar <updated> como data de publicação", B / "noticias.py",
     '_data_iso(e.findtext(_ATOM + "published")),', '_data_iso(e.findtext(_ATOM + "updated")),',
     "tests/boletim/test_noticias_coleta.py::test_atom_usa_published_e_nao_updated_nem_autor"),
    ("horário calculado no fuso do servidor, não no de Brasília", B / "noticias_job.py",
     "    local = agora.astimezone(TZ)\n    dia = local.date() if local.time() >= HORA_DIARIA",
     "    local = agora.astimezone()\n    dia = local.date() if local.time() >= HORA_DIARIA",
     "tests/boletim/test_noticias_agendamento.py::test_fuso_do_servidor_nao_altera_o_horario"),
    ("lease ignorado: qualquer worker reexecuta", B / "noticias_job.py",
     "    if row and row[\"status\"] == \"executando\" and row[\"dono_lease\"] == dono:",
     "    if row:",
     "tests/boletim/test_noticias_agendamento.py::test_dois_workers_ao_mesmo_tempo_buscam_uma_vez"),
    ("transação aberta durante o HTTP", B / "noticias_job.py",
     "    with nt.novo_cliente(transporte) as cliente:          # ← nenhuma transação aberta aqui",
     "    import sqlite3 as _s; _trava = _s.connect(db.DB_PATH); _trava.execute('BEGIN IMMEDIATE')\n"
     "    with nt.novo_cliente(transporte) as cliente:          # ← nenhuma transação aberta aqui",
     "tests/boletim/test_noticias_agendamento.py::test_nenhuma_transacao_aberta_durante_o_http"),
    ("cache vencido continua aparecendo como recente", B / "noticias_job.py",
     "            if pub and inicio <= pub <= limite:", "            if True:",
     "tests/boletim/test_noticias_agendamento.py::test_cache_vencido_nao_apresenta_noticia_antiga_como_recente"),
    ("falha total apaga a edição anterior", B / "noticias_job.py",
     "        ok = [f for f in fontes if resultado[f.id][\"ok\"]]",
     "        conn.execute(\"DELETE FROM noticias_edicoes\")\n        ok = [f for f in fontes if resultado[f.id][\"ok\"]]",
     "tests/boletim/test_noticias_agendamento.py::test_falha_total_preserva_edicao_e_retenta_com_backoff_ate_esgotar"),
    ("segmento vazio cai em psicologia (como normalize_segment)", B / "noticias.py",
     "    return MAPA_SEGMENTO.get(bruto.strip().lower())",
     "    return MAPA_SEGMENTO.get(bruto.strip().lower() or 'psicologia', 'psicologia')",
     "tests/boletim/test_noticias_painel.py::test_segmento_desconhecido_ou_vazio_nao_recebe_psicologia_em_silencio"),
    ("painel coleta a cada acesso", B / "noticias_job.py",
     "    \"\"\"O que o quadro mostra. Nunca acessa a internet.\"\"\"\n    segmento = nt.segmento_do_tenant(tenant)\n    if not segmento:\n        return {\"habilitado\": False}",
     "    \"\"\"O que o quadro mostra. Nunca acessa a internet.\"\"\"\n    segmento = nt.segmento_do_tenant(tenant)\n    if not segmento:\n        return {\"habilitado\": False}\n    with nt.novo_cliente() as _c: nt.baixar(nt.CFP, _c)",
     "tests/boletim/test_noticias_painel.py::test_abrir_o_painel_so_le_o_cache"),
    ("narrativa com IA volta para psicologia", B / "main.py",
     "        if _nt.segmento_do_tenant(tenant):\n            return {\"narrative\": \"\"}",
     "        if False:\n            return {\"narrative\": \"\"}",
     "tests/boletim/test_noticias_painel.py::test_psicologia_nao_gera_mais_a_narrativa_nem_chama_o_modelo"),
    ("quadro usa innerHTML com conteúdo externo", B / "templates" / "dashboard.html",
     "    li.appendChild(t);", "    li.appendChild(t); li.innerHTML += it.titulo;",
     "tests/boletim/test_noticias_painel.py::test_conteudo_externo_so_entra_no_dom_por_textcontent"),
]

falhas = 0
for nome, arq, de, para, teste in MUTACOES:
    original = arq.read_text(encoding="utf-8")
    assert original.count(de) == 1, f"trecho não encontrado (ou repetido) para: {nome}"
    arq.write_text(original.replace(de, para), encoding="utf-8")
    try:
        r = subprocess.run([PY, "-m", "pytest", "-q", "-x", teste], cwd=RAIZ, capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}, timeout=180)
    finally:
        arq.write_text(original, encoding="utf-8")
    pegou = r.returncode != 0
    falhas += not pegou
    print(f"{'PEGOU ' if pegou else 'ESCAPOU'}  {nome}  →  {teste.split('::')[1]}")
print(f"RESULTADO: {len(MUTACOES) - falhas}/{len(MUTACOES)} mutações detectadas")
sys.exit(1 if falhas else 0)
