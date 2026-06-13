import os
import io
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, make_response, send_file
from openai import OpenAI
import json
import re
import time
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime, timedelta
import uuid
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors

# Carrega variáveis de ambiente
load_dotenv()

# Configuração do OpenRouter - use um modelo diferente ou adicione créditos
client = OpenAI(
    api_key='sk-9fa76a5dc8d547e48037eb115fd5e672', # Get from DeepSeek's platform
    base_url="https://api.deepseek.com/v1" # DeepSeek's API endpoint
)

app = Flask(__name__)
CORS(app)

# Estado simples em memória para Planning/ReAct (sessões efêmeras)
SESSOES = {}

# -------------------- Helpers: rotina e cronograma --------------------
DIAS_ORDEM = [
    "segunda", "terca", "terça", "quarta", "quinta", "sexta", "sabado", "sábado", "domingo"
]

def _parse_hhmm(hhmm: str) -> datetime:
    return datetime.strptime(hhmm, "%H:%M")

def _format_hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")

def _duracao_min(inicio: str, fim: str) -> int:
    ini = _parse_hhmm(inicio)
    fi = _parse_hhmm(fim)
    if fi <= ini:
        fi += timedelta(days=1)
    return int((fi - ini).total_seconds() // 60)

def _parse_rotina(rotina_str: str):
    """
    Aceita JSON no formato:
    {
      "segunda": {"inicio": "19:00", "fim": "21:00"},
      ...,
      "domingo": null
    }
    Fallback: se falhar, cria rotina padrão de 2h seg-sex (19-21) e 4h sábado (9-13), domingo folga.
    """
    try:
        obj = json.loads(rotina_str) if isinstance(rotina_str, str) and rotina_str.strip() else rotina_str
        if not isinstance(obj, dict):
            raise ValueError("Rotina inválida")
    except Exception:
        obj = {
            "segunda": {"inicio": "19:00", "fim": "21:00"},
            "terca": {"inicio": "19:00", "fim": "21:00"},
            "quarta": {"inicio": "19:00", "fim": "21:00"},
            "quinta": {"inicio": "19:00", "fim": "21:00"},
            "sexta": {"inicio": "19:00", "fim": "21:00"},
            "sabado": {"inicio": "09:00", "fim": "13:00"},
            "domingo": None,
        }

    rotina = {}
    for dia in DIAS_ORDEM:
        key = dia
        if dia == "terça":
            key = "terca" if "terca" in obj else dia
        if dia == "sábado":
            key = "sabado" if "sabado" in obj else dia
        if key in obj:
            val = obj[key]
            if val and isinstance(val, dict) and "inicio" in val and "fim" in val:
                inicio, fim = val["inicio"], val["fim"]
                try:
                    mins = _duracao_min(inicio, fim)
                except Exception:
                    mins = 0
                rotina[dia] = {"inicio": inicio, "fim": fim, "minutos": mins}
            else:
                rotina[dia] = None
        else:
            rotina[dia] = None
    return rotina

def _collect_disciplinas(conteudos_json: dict):
    """Normaliza disciplinas com topicos e peso. Default: Específicos=2.0; demais=1.0."""
    disciplinas = []
    gerais = conteudos_json.get("gerais", {}) or {}
    especificos = conteudos_json.get("especificos", {}) or {}

    def _explode_topicos(lista):
        norm = []
        for item in (lista or []):
            if isinstance(item, str):
                # Divide por ; ou \n quando a IA retorna um bloco único com muitos tópicos
                partes = re.split(r"[;\n]\s*", item.strip())
                for p in partes:
                    p = p.strip().strip(',').strip()
                    if p:
                        norm.append(p)
            else:
                if item:
                    norm.append(str(item))
        return norm

    for nome, val in gerais.items():
        if isinstance(val, dict):
            topicos = _explode_topicos(val.get("topicos", []))
            peso = float(val.get("peso", 1.0))
        else:
            topicos = _explode_topicos(val or [])
            peso = 1.0
        disciplinas.append({"nome": nome, "topicos": list(topicos), "peso": peso})

    for nome, val in especificos.items():
        if isinstance(val, dict):
            topicos = _explode_topicos(val.get("topicos", []))
            peso = float(val.get("peso", 2.0))
        else:
            topicos = _explode_topicos(val or [])
            peso = 2.0
        disciplinas.append({"nome": nome, "topicos": list(topicos), "peso": peso})

    # Normalização de pesos
    soma = sum(d["peso"] for d in disciplinas) or 1.0
    for d in disciplinas:
        d["peso_norm"] = d["peso"] / soma
        d["idx_topico"] = 0
        d["credit"] = 0.0  # para seleção ponderada por bloco
        d["toggle_tipo"] = "teoria"  # alternar teoria/questoes
    return disciplinas

def _selecionar_disciplina(disciplinas):
    # Weighted Fair Queuing simplificado
    for d in disciplinas:
        d["credit"] += d["peso_norm"]
    escolhida = max(disciplinas, key=lambda x: x["credit"])
    escolhida["credit"] -= 1.0
    return escolhida

def _next_topicos(d, max_itens=2):
    if not d["topicos"]:
        return []
    tps = []
    for _ in range(max_itens):
        tps.append(d["topicos"][d["idx_topico"] % max(1, len(d["topicos"]))])
        d["idx_topico"] = (d["idx_topico"] + 1) % max(1, len(d["topicos"]))
    return tps

def _gerar_blocos_do_dia(inicio_str, fim_str, disciplinas, revisao_ref=None):
    """Gera blocos 50/10 e insere revisão 15 min no início se houver revisao_ref."""
    if not inicio_str or not fim_str:
        return []
    inicio_dt = _parse_hhmm(inicio_str)
    fim_dt = _parse_hhmm(fim_str)
    if fim_dt <= inicio_dt:
        fim_dt += timedelta(days=1)

    atual = inicio_dt
    blocos = []

    # Revisão 24h
    if revisao_ref:
        rev_fim = atual + timedelta(minutes=15)
        blocos.append({
            "inicio": _format_hhmm(atual),
            "fim": _format_hhmm(rev_fim),
            "tipo": "revisao",
            "referencia": revisao_ref
        })
        atual = rev_fim + timedelta(minutes=5)  # pequena transição

    # Blocos de estudo 50/10
    while atual + timedelta(minutes=30) <= fim_dt:  # garantir bloco útil mínimo
        bloco_fim = min(fim_dt, atual + timedelta(minutes=50))
        d = _selecionar_disciplina(disciplinas)
        topicos = _next_topicos(d, max_itens=2)
        tipo = d["toggle_tipo"]
        d["toggle_tipo"] = "questoes" if d["toggle_tipo"] == "teoria" else "teoria"
        blocos.append({
            "inicio": _format_hhmm(atual),
            "fim": _format_hhmm(bloco_fim),
            "disciplina": d["nome"],
            "topicos": topicos,
            "tipo": tipo
        })
        atual = bloco_fim
        if atual + timedelta(minutes=10) < fim_dt:
            atual += timedelta(minutes=10)  # pausa 10m
        else:
            break

    return blocos

def montar_plano(conteudos_json: dict, rotina_str: str, cargo: str):
    rotina = _parse_rotina(rotina_str)
    disciplinas = _collect_disciplinas(conteudos_json)

    dias_saida = []
    studied_refs_por_dia = []

    # Data da semana (label)
    hoje = datetime.now()
    inicio_semana = hoje - timedelta(days=hoje.weekday())
    fim_semana = inicio_semana + timedelta(days=6)
    semana_label = f"{inicio_semana.strftime('%d/%m')} à {fim_semana.strftime('%d/%m')}"

    # Mapeia nomes normalizados dos dias para índice
    dias_norm = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]

    # Converte chaves da rotina para nomes normalizados
    rotina_norm = {}
    for k, v in rotina.items(): 
        kn = "terca" if k in ("terca", "terça") else ("sabado" if k in ("sabado", "sábado") else k)
        rotina_norm[kn] = v

    prev_study_ref = None
    for dia in dias_norm:
        info = rotina_norm.get(dia)
        if info:
            revisao_ref = prev_study_ref
            blocos = _gerar_blocos_do_dia(info["inicio"], info["fim"], disciplinas, revisao_ref=revisao_ref)
            # Atualiza referência de revisão para o próximo dia (último bloco estudado do dia)
            refs = [b for b in blocos if b.get("disciplina")]
            if refs:
                last = refs[-1]
                prev_study_ref = f"{last['disciplina']} - {', '.join(last.get('topicos', [])[:2])}"
            else:
                prev_study_ref = None
            dias_saida.append({
                "dia": dia,
                "inicio": info["inicio"],
                "fim": info["fim"],
                "blocos": blocos
            })
            studied_refs_por_dia.append(prev_study_ref)
        else:
            dias_saida.append({"dia": dia, "folga": True, "blocos": []})
            studied_refs_por_dia.append(None)

    # Resumo horas
    total_min = 0
    por_disc = {}
    for d in dias_saida:
        for b in d.get("blocos", []):
            ini = _parse_hhmm(b["inicio"]) if b.get("inicio") else None
            fim = _parse_hhmm(b["fim"]) if b.get("fim") else None
            if not ini or not fim:
                continue
            dur = int((fim - ini).total_seconds() // 60)
            if b.get("tipo") != "revisao":
                total_min += dur
                disc = b.get("disciplina")
                por_disc[disc] = por_disc.get(disc, 0) + dur

    resumo = {
        "horas_totais": round(total_min / 60.0, 2),
        "por_disciplina": {k: round(v / 60.0, 2) for k, v in por_disc.items()}
    }

    return {
        "cargo": cargo,
        "semana": semana_label,
        "semana_inicio": inicio_semana.strftime('%Y-%m-%d'),
        "semana_fim": fim_semana.strftime('%Y-%m-%d'),
        "dias": dias_saida,
        "resumo": resumo,
        "conteudos": conteudos_json
    }

# -------------------- Reflection: revisão do JSON de disciplinas --------------------
def revisar_conteudos_reflection(conteudos_json: dict, cargo: str, texto_limitado: str) -> dict:
    try:
        prompt_reflect = f"""
        Você é um verificador crítico de extrações de edital para o cargo: {cargo}.
        1) Verifique se o JSON abaixo contém TODAS as disciplinas e tópicos coerentes com o cargo informado.
        2) Se algo estiver faltando, incorreto ou de outro nível, CORRIJA.
        3) Mantenha o formato EXATO abaixo, apenas o objeto JSON final, sem texto externo.

        CONTEXTO (trecho do edital):\n{texto_limitado[:3500]}

        JSON ATUAL:\n{json.dumps(conteudos_json, ensure_ascii=False)}

        Responda SOMENTE com o JSON corrigido, no formato:
        {{
          "gerais": {{ "Disciplina": ["t1", "t2", ...] }},
          "especificos": {{ "Conhecimentos Específicos": ["t1", ...] }}
        }}
        """
        time.sleep(1)
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Você é rigoroso, factual e responde somente com JSON válido."},
                {"role": "user", "content": prompt_reflect}
            ],
            temperature=0.1
        )
        texto = resp.choices[0].message.content.strip()
        texto = texto.replace('```json', '').replace('```', '').strip()
        # Tenta carregar JSON; se falhar, mantém original
        return json.loads(texto)
    except Exception:
        return conteudos_json

# -------------------- PDF --------------------
def _fmt(v):
    return v if v is not None else "—"

def extrair_metadados_edital(texto: str) -> dict:
    # Heurísticas simples; podem falhar dependendo do edital
    meta = {
        "edital_numero": None,
        "concurso_nome": None,
        "salario": None,
        "banca": None,
        "inscricoes_inicio": None,
        "inscricoes_inicio_hora": None,
        "inscricoes_ate": None,
        "inscricoes_ate_hora": None,
        "taxa": None,
        "vagas": None,
        "vagas_detalhe": None,
        "prova_data": None,
    }
    try:
        m = re.search(r"Edital\s*(N[ºo]\s*[^\n]+)", texto, flags=re.I)
        if m: meta["edital_numero"] = m.group(1).strip()
        m = re.search(r"Prefeitura\s+(Municipal\s+de\s+)?([^\n\-]+)", texto, flags=re.I)
        if m: meta["concurso_nome"] = f"Prefeitura de {m.group(2).strip()}"
        m = re.search(r"Sal[áa]rio\s*R\$\s*([\d\.,]+(?:\s*a\s*R\$\s*[\d\.,]+)?)", texto, flags=re.I)
        if m: meta["salario"] = f"R$ {m.group(1).strip()}"
        m = re.search(r"Banca\s*[:\-]?\s*([A-ZÇÃÉÍÓÚ\-\. ]{2,})", texto, flags=re.I)
        if m: meta["banca"] = m.group(1).strip()
        # Período de inscrições: início e fim com hora, quando houver
        m = re.search(r"Inscri[cç][õo]es?\s*(?:de|do\s*dia)\s*([\d/]{8,10})(?:\s*(?:às|a partir das)\s*([\d:]{4,5}))?\s*(?:at[eé]\s*|a\s*|\-)\s*([\d/]{8,10})(?:\s*(?:às)\s*([\d:]{4,5}))?", texto, flags=re.I)
        if m:
            meta["inscricoes_inicio"] = m.group(1).strip()
            if m.group(2): meta["inscricoes_inicio_hora"] = m.group(2).strip()
            meta["inscricoes_ate"] = m.group(3).strip()
            if m.group(4): meta["inscricoes_ate_hora"] = m.group(4).strip()
        else:
            m = re.search(r"Inscri[cç][õo]es?\s*(?:at[eé])\s*([\d/]{8,10})", texto, flags=re.I)
            if m: meta["inscricoes_ate"] = m.group(1).strip()
        m = re.search(r"Taxa\s*de\s*inscri[cç][ãa]o\s*R\$\s*([\d\.,]+(?:\s*a\s*R\$\s*[\d\.,]+)?)", texto, flags=re.I)
        if m: meta["taxa"] = f"R$ {m.group(1).strip()}"
        m = re.search(r"Vagas?\s*:?\s*(\d{1,5})", texto, flags=re.I)
        if m: meta["vagas"] = m.group(1)
        # Detalhamento de vagas (heurístico)
        detalhes = []
        for rotulo in [r"ampla concorr[êe]ncia", r"PcD", r"pessoas com defici[êe]ncia", r"negros", r"cotas"]:
            mm = re.search(rf"(\d+)\s*(?:vagas?\s*)?(?:para\s*)?{rotulo}", texto, flags=re.I)
            if mm:
                detalhes.append(f"{mm.group(1)} {rotulo}")
        if detalhes:
            meta["vagas_detalhe"] = "; ".join(detalhes)
        m = re.search(r"Data\s*da\s*prova\s*[:\-]?\s*([\d/]{8,10})", texto, flags=re.I)
        if m: meta["prova_data"] = m.group(1)
    except Exception:
        pass
    return meta

def gerar_pdf_plano(info_concurso: dict, plano: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    story = []

    # Título e subtítulo
    prova_info = info_concurso.get('prova_data')
    concurso_nome = info_concurso.get('concurso_nome') or 'Concurso'
    titulo = f"{concurso_nome} — {_fmt(info_concurso.get('edital_numero') or 'Edital')}"
    if prova_info:
        titulo += f" — Prova em {prova_info}"
    story.append(Paragraph(titulo, styles['Title']))
    subtitulo = f"Plano de estudo para o cargo {plano.get('cargo', '')} — Semana: {plano.get('semana', '')}"
    story.append(Paragraph(subtitulo, styles['Heading3']))
    story.append(Spacer(1, 6))

    # Cabeçalho semelhante ao exemplo dado
    # Monta período de inscrições
    inscr_ini = info_concurso.get('inscricoes_inicio')
    inscr_ini_h = info_concurso.get('inscricoes_inicio_hora')
    inscr_fim = info_concurso.get('inscricoes_ate')
    inscr_fim_h = info_concurso.get('inscricoes_ate_hora')
    if inscr_ini or inscr_fim:
        periodo = []
        if inscr_ini:
            periodo.append(inscr_ini + (f" {inscr_ini_h}" if inscr_ini_h else ""))
        if inscr_fim:
            conj = " até " if periodo else "Até "
            periodo.append(conj + inscr_fim + (f" {inscr_fim_h}" if inscr_fim_h else ""))
        periodo_str = "".join(periodo)
    else:
        periodo_str = _fmt(None)

    vagas_str = info_concurso.get('vagas')
    if info_concurso.get('vagas_detalhe'):
        vagas_str = f"{_fmt(vagas_str)} (" + info_concurso['vagas_detalhe'] + ")"

    dados_topo = [
        ["Salário", _fmt(info_concurso.get('salario'))],
        ["Banca", _fmt(info_concurso.get('banca'))],
        ["Inscrições", periodo_str],
        ["Taxa de inscrição", _fmt(info_concurso.get('taxa'))],
        ["Vagas", _fmt(vagas_str)],
        ["Data da prova", _fmt(info_concurso.get('prova_data'))],
    ]
    tabela = Table(dados_topo, colWidths=[50*mm, 110*mm])
    tabela.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
        ('BACKGROUND', (0,0), (-1,0), colors.whitesmoke),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(tabela)
    story.append(Spacer(1, 10))

    story.append(Paragraph("Guia de estudo por cargo", styles['Heading2']))
    story.append(Paragraph(f"{plano.get('cargo')} — Semana: {plano.get('semana')}", styles['Normal']))
    story.append(Spacer(1, 8))

    # Seção de conteúdos completos
    conteudos = plano.get('conteudos') or {}
    if conteudos:
        story.append(Paragraph(f"Conteúdos para o cargo {plano.get('cargo')}", styles['Heading2']))
        # Disciplinas gerais
        gerais = conteudos.get('gerais') or {}
        for disc, itens in gerais.items():
            story.append(Paragraph(disc, styles['Heading4']))
            if isinstance(itens, dict):
                itens = itens.get('topicos', [])
            texto = "; ".join([str(i).strip().strip(',') for i in itens if str(i).strip()])
            story.append(Paragraph(texto, styles['Normal']))
            story.append(Spacer(1, 2))
        # Específicos
        especificos = conteudos.get('especificos') or {}
        for disc, itens in especificos.items():
            story.append(Paragraph(disc, styles['Heading4']))
            if isinstance(itens, dict):
                itens = itens.get('topicos', [])
            texto = "; ".join([str(i).strip().strip(',') for i in itens if str(i).strip()])
            story.append(Paragraph(texto, styles['Normal']))
            story.append(Spacer(1, 4))
        story.append(Spacer(1, 8))

    # Resumo
    story.append(Paragraph("Resumo de horas", styles['Heading3']))
    resumo = plano.get('resumo', {})
    linhas = [["Total (h)", str(resumo.get('horas_totais', '0'))]]
    for disc, h in (resumo.get('por_disciplina') or {}).items():
        linhas.append([disc, str(h)])
    tabela_resumo = Table(linhas, colWidths=[100*mm, 60*mm])
    tabela_resumo.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
        ('BACKGROUND', (0,0), (-1,0), colors.whitesmoke),
    ]))
    story.append(tabela_resumo)
    story.append(Spacer(1, 8))

    # Cronograma semanal
    story.append(Paragraph("Plano de estudos (blocos)", styles['Heading3']))
    for dia in plano.get('dias', []):
        if dia.get('folga'):
            story.append(Paragraph(f"{dia['dia'].title()}: folga", styles['Normal']))
            continue
        story.append(Paragraph(f"{dia['dia'].title()} — {dia.get('inicio')} às {dia.get('fim')}", styles['Heading4']))
        for b in dia.get('blocos', []):
            if b.get('tipo') == 'revisao':
                story.append(Paragraph(f"{b['inicio']}–{b['fim']}: Revisão (24h) — {b.get('referencia', '')}", styles['Normal']))
            else:
                top = ", ".join(b.get('topicos', [])[:2])
                story.append(Paragraph(f"{b['inicio']}–{b['fim']}: {b.get('disciplina')} — {b.get('tipo')} — {top}", styles['Normal']))
        story.append(Spacer(1, 4))

    doc.build(story)
    buf. seek(0)
    return buf.read()

@app.route('/extrair_cargos', methods=['POST'])
def extrair_cargos():
    if 'edital' not in request.files:
        return jsonify({"erro": "Nenhum edital enviado"}), 400

    arquivo = request.files["edital"]
    texto = ""
    try:
        with fitz.open(stream=arquivo.read(), filetype="pdf") as pdf:
            for pagina in pdf:
                texto += pagina.get_text()
    except Exception as e:
        return jsonify({"erro": f"Erro ao ler PDF: {str(e)}"}), 500

    cargos = re.findall(r'Cargo\s*:\s*(.+)', texto)
    cargos = list(set([c.strip() for c in cargos if c.strip()]))

    return jsonify({"cargos": cargos})

@app.route('/gerar', methods=['POST'])
def gerar_plano():
    # Verifica se um edital foi enviado. Se não, trata como um plano genérico.
    if 'edital' not in request.files:
        return jsonify({"erro": "Nenhum edital enviado"}), 400

    rotina = request.form.get("rotina", "")
    cargo = request.form.get("cargo", "")
    arquivo = request.files["edital"]

    if not rotina.strip() or not cargo.strip():
        return jsonify({"erro": "Dados incompletos"}), 400

    # Extrair texto do PDF
    texto = ""
    try:
        with fitz.open(stream=arquivo.read(), filetype="pdf") as pdf:
            for pagina in pdf:
                texto += pagina.get_text()
    except Exception as e:
        return jsonify({"erro": f"Erro ao ler PDF: {str(e)}"}), 500

    # Limitar tamanho do texto
    texto_limitado = texto[:6000]

    # Primeiro prompt - extrair conteúdos
    prompt_extrair = f"""
        ANÁLISE ESTRUTURAL DO EDITAL:
        1. PRIMEIRO identifique o NÍVEL do cargo "{cargo}" (Fundamental, Médio/Técnico ou Superior) consultando o Quadro de Cargos (páginas 2-8)
        2. LOCALIZE a tabela de distribuição de disciplinas CORRETA baseada no nível:
        - Se o cargo for "Administrador", "Contador" ou "Analista de Sistemas", use a tabela da página 20.
        - Para os demais cargos de nível Superior, use a tabela da página 21.
        - Para cargos de Nível Médio e Técnico, use a tabela da página 21.
        - Para cargos de Nível Fundamental, use a tabela da página 20.
        3. SEPARE claramente:
        - Disciplinas GERAIS (comuns a vários cargos)
        - Conhecimentos ESPECÍFICOS (únicos para este cargo)

        INSTRUÇÕES DE EXTRAÇÃO:
        - Para disciplinas GERAIS: Busque os tópicos na seção "CARGOS DE NÍVEL [NÍVEL]" (páginas 45-50) para cada disciplina.
        - Para ESPECÍFICOS: Busque em "Conhecimentos Específicos - {cargo}" (páginas 46 em diante) e extraia os tópicos listados.
        - IGNORE disciplinas de outros níveis.
        - EXTRAIA os tópicos EXATAMENTE como aparecem no edital, sem resumir ou inventar.

        EXTRATOS EXEMPLO DO EDITAL:
        Tabela (página 20 para Analista de Sistemas):
        |DISCIPLINA|NÚMERO DE QUESTÕES|PESO POR QUESTÃO|PONTUAÇÃO MÁXIMA|
        |---|---|---|---|
        |Língua Portuguesa|10|2,0|20,0|
        |Raciocínio Lógico e Matemático|5|1,0|5,0|
        |Legislação do Sistema Único de Saúde - SUS|5|1,0|5,0|
        |Conhecimentos do Município de Contagem-MG|5|1,0|5,0|
        |Conhecimentos Específicos|15|2,0|30,0|

        Conteúdo programático (página 49-50):
        - Língua Portuguesa (nível Superior): "Regência verbal e nominal; estudo da crase; semântica e estilística; ..."
        - Conhecimentos Específicos - Analista de Sistemas (página 50): "Lógica de Programação: Construção de algoritmos; ..."

        FORMATO EXIGIDO:
        {{
        "gerais": {{
            "Língua Portuguesa": ["tópico1", "tópico2", ...],
            "Raciocínio Lógico e Matemático": ["tópico1", "tópico2", ...],
            "Legislação do Sistema Único de Saúde - SUS": ["tópico1", "tópico2", ...],
            "Conhecimentos do Município de Contagem-MG": ["tópico1", "tópico2", ...]
        }},
        "especificos": {{
            "Conhecimentos Específicos": ["tópico1", "tópico2", ...]
        }}
        }}

        CARGO ALVO: "{cargo}"
        NÍVEL IDENTIFICADO: [preencher aqui]
        TABELA UTILIZADA: [preencher aqui]

        EXTRAIR AGORA:
    """

    try:
        # Adicionar delay para evitar rate limit
        time.sleep(2)
        
        # Tente usar um modelo diferente do OpenRouter
        response_extrair = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": "Você é um especialista em análise de editais de concurso público."},
                {"role": "user", "content": prompt_extrair}
            ],
            temperature=0.1
        )
        
        conteudos = response_extrair.choices[0].message.content.strip()
        print(conteudos.encode('utf-8', errors='replace').decode('utf-8', errors='replace'))

        # Tentar parsear JSON de várias formas, retornando erro claro se falhar
        try:
            # Limpar resposta para extrair apenas o JSON (removendo fences)
            conteudos_limpo = conteudos.replace('```json', '').replace('```', '').strip()
            conteudos_json = json.loads(conteudos_limpo)
        except Exception as e:
            print(f"Erro ao parsear JSON diretamente: {e}")
            # Tentativa alternativa: pegar o primeiro e o último brace e carregar
            try:
                first = conteudos.find('{')
                last = conteudos.rfind('}')
                if first != -1 and last != -1 and last > first:
                    possible = conteudos[first:last+1]
                    conteudos_json = json.loads(possible)
                    print("Parse alternativo bem-sucedido usando substring JSON.")
                else:
                    raise ValueError("Não encontrou delimitadores JSON adequados")
            except Exception as e2:
                print(f"Falha no parse alternativo: {e2}")
                # Retorna erro para frontend com informação de debug
                resp = make_response(jsonify({"erro": "Falha ao extrair conteúdos do edital.",
                                              "detalhe_parse": str(e2),
                                              "resposta_raw": conteudos[:2000]}), 500)
                resp.headers["Access-Control-Allow-Origin"] = "*"
                return resp

        # Reflection: segunda passada para revisar/ajustar disciplinas
        conteudos_json = revisar_conteudos_reflection(conteudos_json, cargo, texto_limitado)

        # Verificar se obteve disciplinas; se não, abortar com mensagem clara
        disciplinas_test = _collect_disciplinas(conteudos_json)
        if not disciplinas_test:
            print("Nenhuma disciplina extraída (conteudos_json está vazio ou mal formado).")
            resp = make_response(jsonify({"erro": "Nenhuma disciplina extraída do edital.",
                                          "resposta_raw": conteudos[:2000]}), 500)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        # Gerar plano determinístico localmente a partir de conteudos_json e rotina
        plano = montar_plano(conteudos_json, rotina, cargo)

        # Extrair metadados do edital
        info = extrair_metadados_edital(texto)

        # Gerar PDF
        pdf_bytes = gerar_pdf_plano(info, plano)
        filename = f"plano_{cargo.replace(' ', '_')}.pdf"
        return send_file(io.BytesIO(pdf_bytes), mimetype='application/pdf', as_attachment=True, download_name=filename)
    
    except Exception as e:
        print(f"Erro completo: {e}")
        return jsonify({"erro": f"Erro na API: {str(e)}"}), 500

@app.route('/teste', methods=['GET'])
def teste():

    return jsonify({"status": "Servidor funcionando!"})

# -------------------- Planning/ReAct: endpoint interativo --------------------
@app.route('/informar', methods=['POST'])
def informar():
    """
    Loop interativo simples:
    - Sem session_id: inicia e pergunta Disciplinas.
    - Depois: Banca -> Método -> Avaliação. Ao final, retorna um plano-base de perguntas e respostas.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        session_id = data.get('session_id')
        answer = data.get('answer')
        rotina = data.get('rotina', '')
        cargo = data.get('cargo', '')

        etapas = [
            ("disciplinas", "1) Quais são as disciplinas para o cargo? Liste-as."),
            ("banca", "2) Qual é a banca do concurso?"),
            ("metodo", "3) Qual método científico você deseja seguir (ex.: 50/10, 25/5, SRS)?"),
            ("avaliacao", "4) Como a eficiência do plano será avaliada (ex.: questões/semana, % de acertos)?"),
        ]

        if not session_id:
            session_id = str(uuid.uuid4())
            SESSOES[session_id] = {"estado": 0, "respostas": {}, "rotina": rotina, "cargo": cargo}
            prox = etapas[0][1]
            return jsonify({"session_id": session_id, "next_question": prox})

        sess = SESSOES.get(session_id)
        if not sess:
            return jsonify({"erro": "Sessão inválida. Reinicie."}), 400

        idx = sess["estado"]
        if idx < len(etapas) and answer is not None:
            chave = etapas[idx][0]
            sess["respostas"][chave] = answer
            sess["estado"] = idx + 1

        idx = sess["estado"]
        if idx < len(etapas):
            prox = etapas[idx][1]
            return jsonify({"session_id": session_id, "next_question": prox, "partial": sess["respostas"]})

        # Finaliza: monta um plano simples usando as respostas do usuário e a rotina
        respostas = sess["respostas"]
        plano_usuario = {
            "cargo": sess.get("cargo"),
            "rotina": sess.get("rotina"),
            "disciplinas": respostas.get("disciplinas"),
            "banca": respostas.get("banca"),
            "metodo": respostas.get("metodo"),
            "avaliacao": respostas.get("avaliacao"),
            "observacoes": "Plano criado via fluxo Planning/ReAct. Ajuste conforme necessário."
        }
        # Reflection final: pergunta se algo está faltando
        prompt_final = f"""
        Verifique se o plano abaixo está completo e consistente com o cargo {sess.get('cargo')}.
        Se algo importante estiver faltando, acrescente no campo 'observacoes'. Responda só com JSON.
        {json.dumps(plano_usuario, ensure_ascii=False)}
        """
        try:
            time.sleep(1)
            r = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Você é um revisor sucinto que retorna apenas JSON."},
                    {"role": "user", "content": prompt_final}
                ],
                temperature=0.1
            )
            plano_usuario = json.loads(r.choices[0].message.content.strip().replace('```json','').replace('```',''))
        except Exception:
            pass

        # Encerra sessão
        SESSOES.pop(session_id, None)
        return jsonify({"resultado": plano_usuario, "message": "Planejamento concluído."})
    except Exception as e:
        return jsonify({"erro": f"Falha no fluxo interativo: {str(e)}"}), 500

if __name__ == '__main__':
    print("Iniciando servidor Flask...")
    app.run(host="0.0.0.0", port=5000, debug=True)