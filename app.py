"""
app.py — Backend Flask para geração de planos de estudo personalizados
para concursos públicos.

Fluxo principal:
  1. O usuário envia o PDF do edital, o cargo desejado e sua rotina de estudos.
  2. A IA (DeepSeek) extrai as disciplinas e tópicos do edital.
  3. Uma segunda passagem ("reflection") revisa e corrige a extração.
  4. Um algoritmo determinístico gera blocos de estudo (50 min estudo / 10 min pausa).
  5. O resultado é exportado como PDF para download.

Endpoints:
  GET  /teste          → Verifica se o servidor está no ar.
  POST /extrair_cargos → Retorna lista de cargos encontrados no edital (PDF).
  POST /gerar          → Gera e retorna o PDF do plano de estudos.
  POST /informar       → Fluxo interativo (perguntas e respostas) para montar um plano sem edital.
"""

import io
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta

import fitz  # PyMuPDF — leitura de PDF
from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, request, send_file
from flask_cors import CORS
from openai import OpenAI
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# ---------------------------------------------------------------------------
# Configuração inicial
# ---------------------------------------------------------------------------

load_dotenv()
chave_deepseek = os.environ.get("DEEPSEEK_API_KEY")

if not chave_deepseek:
    raise RuntimeError(
        "Variável de ambiente DEEPSEEK_API_KEY não definida. "
        "Defina-a no sistema ou no arquivo .env antes de iniciar o servidor."
    )

# Cliente da API DeepSeek (compatível com interface OpenAI)
cliente_ia = OpenAI(
    api_key=chave_deepseek,
    base_url="https://api.deepseek.com/v1",
)

app = Flask(__name__)
CORS(app)

# Armazena sessões temporárias do fluxo interativo (/informar) em memória.
# Chave: session_id (UUID) → Valor: dicionário com estado da conversa.
sessoes_ativas: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

MODELO_IA = "deepseek-chat"

# Ordem canônica dos dias da semana (usada para montar o cronograma)
ORDEM_DIAS_SEMANA = [
    "segunda", "terca", "quarta", "quinta",
    "sexta", "sabado", "domingo",
]

# Duração dos blocos de estudo e pausas (em minutos)
DURACAO_BLOCO_ESTUDO_MIN = 50
DURACAO_PAUSA_MIN = 10
DURACAO_REVISAO_DIA_ANTERIOR_MIN = 15
DURACAO_MINIMA_BLOCO_UTIL_MIN = 30  # descarta blocos muito curtos no fim do horário

# ---------------------------------------------------------------------------
# Seção 1 — Utilitários de horário
# ---------------------------------------------------------------------------

def horario_para_datetime(horario_hhmm: str) -> datetime:
    """Converte uma string 'HH:MM' em objeto datetime (data base = hoje)."""
    return datetime.strptime(horario_hhmm, "%H:%M")


def datetime_para_horario(dt: datetime) -> str:
    """Converte um objeto datetime de volta para string 'HH:MM'."""
    return dt.strftime("%H:%M")


def calcular_duracao_minutos(horario_inicio: str, horario_fim: str) -> int:
    """
    Calcula a duração em minutos entre dois horários 'HH:MM'.
    Suporta virada de meia-noite (ex.: 23:00 → 01:00 = 120 min).
    """
    inicio_dt = horario_para_datetime(horario_inicio)
    fim_dt = horario_para_datetime(horario_fim)
    if fim_dt <= inicio_dt:
        fim_dt += timedelta(days=1)
    return int((fim_dt - inicio_dt).total_seconds() // 60)


# ---------------------------------------------------------------------------
# Seção 2 — Parsing da rotina semanal do usuário
# ---------------------------------------------------------------------------

def parse_rotina_semanal(rotina_raw) -> dict:
    """
    Interpreta a rotina do usuário e retorna um dicionário com os horários por dia.

    Formato esperado (JSON string ou dict):
    {
        "segunda": {"inicio": "19:00", "fim": "21:00"},
        "terca":   {"inicio": "19:00", "fim": "21:00"},
        ...
        "domingo": null   ← null = folga
    }

    Se o valor recebido for inválido ou vazio, aplica uma rotina padrão:
    - Segunda a sexta: 19h–21h (2h/dia)
    - Sábado: 9h–13h (4h)
    - Domingo: folga
    """
    ROTINA_PADRAO = {
        "segunda": {"inicio": "19:00", "fim": "21:00"},
        "terca":   {"inicio": "19:00", "fim": "21:00"},
        "quarta":  {"inicio": "19:00", "fim": "21:00"},
        "quinta":  {"inicio": "19:00", "fim": "21:00"},
        "sexta":   {"inicio": "19:00", "fim": "21:00"},
        "sabado":  {"inicio": "09:00", "fim": "13:00"},
        "domingo": None,
    }

    try:
        dados_rotina = json.loads(rotina_raw) if isinstance(rotina_raw, str) and rotina_raw.strip() else rotina_raw
        if not isinstance(dados_rotina, dict):
            raise ValueError("Rotina não é um dicionário.")
    except Exception:
        dados_rotina = ROTINA_PADRAO

    rotina_normalizada = {}
    for dia in ORDEM_DIAS_SEMANA:
        # Lida com acentuação variável (ex.: "terça" e "terca", "sábado" e "sabado")
        chave_alternativa = "terca" if dia == "terça" else ("sabado" if dia == "sábado" else dia)
        chave_encontrada = dia if dia in dados_rotina else chave_alternativa

        valor = dados_rotina.get(chave_encontrada)
        if valor and isinstance(valor, dict) and "inicio" in valor and "fim" in valor:
            inicio = valor["inicio"]
            fim = valor["fim"]
            try:
                minutos_disponiveis = calcular_duracao_minutos(inicio, fim)
            except Exception:
                minutos_disponiveis = 0
            rotina_normalizada[dia] = {
                "inicio": inicio,
                "fim": fim,
                "minutos_disponiveis": minutos_disponiveis,
            }
        else:
            rotina_normalizada[dia] = None  # Dia de folga

    return rotina_normalizada


# ---------------------------------------------------------------------------
# Seção 3 — Parsing e normalização das disciplinas extraídas do edital
# ---------------------------------------------------------------------------

def normalizar_lista_topicos(lista_bruta: list) -> list[str]:
    """
    Recebe uma lista de tópicos (que pode conter strings com múltiplos itens
    separados por ';' ou '\\n') e retorna uma lista limpa com um tópico por item.
    """
    topicos_normalizados = []
    for item in lista_bruta or []:
        # Divide por ponto-e-vírgula ou quebra de linha (a IA às vezes agrupa tópicos)
        partes = re.split(r"[;\n]\s*", str(item).strip())
        for parte in partes:
            parte_limpa = parte.strip().strip(",").strip()
            if parte_limpa:
                topicos_normalizados.append(parte_limpa)
    return topicos_normalizados


def montar_lista_disciplinas(conteudos_extraidos: dict, dificuldade: str = "") -> list[dict]:
    """
    Transforma o JSON de conteúdos extraídos em uma lista estruturada de disciplinas,
    cada uma com seus tópicos e peso relativo para distribuição de tempo.

    Pesos padrão:
    - Conhecimentos Gerais: 1.0
    - Conhecimentos Específicos: 2.0 (maior ênfase, pois diferencia candidatos)

    Se o parâmetro 'dificuldade' for informado, disciplinas mencionadas como
    difíceis/importantes recebem um boost de 50% no peso.

    Também inicializa contadores usados pelo algoritmo de seleção (WFQ).
    """
    disciplinas = []

    secao_gerais = conteudos_extraidos.get("gerais") or {}
    secao_especificos = conteudos_extraidos.get("especificos") or {}

    def _extrair_topicos_e_peso(dados_disciplina, peso_padrao: float):
        if isinstance(dados_disciplina, dict):
            topicos = normalizar_lista_topicos(dados_disciplina.get("topicos", []))
            peso = float(dados_disciplina.get("peso", peso_padrao))
        else:
            topicos = normalizar_lista_topicos(dados_disciplina or [])
            peso = peso_padrao
        return topicos, peso

    for nome_disciplina, dados in secao_gerais.items():
        topicos, peso = _extrair_topicos_e_peso(dados, peso_padrao=1.0)
        disciplinas.append({
            "nome": nome_disciplina,
            "topicos": topicos,
            "peso_original": peso,
        })

    for nome_disciplina, dados in secao_especificos.items():
        topicos, peso = _extrair_topicos_e_peso(dados, peso_padrao=2.0)
        disciplinas.append({
            "nome": nome_disciplina,
            "topicos": topicos,
            "peso_original": peso,
        })

    # --- Ajuste de pesos com base na dificuldade informada ---
    if dificuldade.strip():
        dificuldade_lower = dificuldade.lower()
        for disciplina in disciplinas:
            nome_lower = disciplina["nome"].lower()
            # Verifica se o nome da disciplina aparece no texto de dificuldade
            if nome_lower in dificuldade_lower:
                # Aplica boost de 50% no peso
                disciplina["peso_original"] *= 1.5

    # Normaliza pesos para que a soma seja 1.0 (facilita o WFQ)
    soma_pesos = sum(d["peso_original"] for d in disciplinas) or 1.0
    for disciplina in disciplinas:
        disciplina["peso_normalizado"] = disciplina["peso_original"] / soma_pesos
        disciplina["credito_wfq"] = 0.0        # Acumulador do algoritmo WFQ
        disciplina["indice_topico_atual"] = 0   # Próximo tópico a usar (circular)
        disciplina["proximo_tipo_sessao"] = "teoria"  # Alterna: teoria → questões → teoria...

    return disciplinas


# ---------------------------------------------------------------------------
# Seção 4 — Algoritmo de seleção de disciplina (Weighted Fair Queuing)
# ---------------------------------------------------------------------------

def selecionar_proxima_disciplina(disciplinas: list[dict]) -> dict:
    """
    Seleciona qual disciplina estudar no próximo bloco usando o algoritmo
    Weighted Fair Queuing (WFQ) simplificado.

    Cada disciplina acumula crédito proporcional ao seu peso. A que tem
    mais crédito acumulado é escolhida e tem 1.0 descontado do seu crédito.

    Isso garante que disciplinas com maior peso recebam mais blocos ao longo
    da semana, de forma justa e distribuída.
    """
    for disciplina in disciplinas:
        disciplina["credito_wfq"] += disciplina["peso_normalizado"]

    disciplina_escolhida = max(disciplinas, key=lambda d: d["credito_wfq"])
    disciplina_escolhida["credito_wfq"] -= 1.0
    return disciplina_escolhida


def obter_proximos_topicos(disciplina: dict, quantidade: int = 2) -> list[str]:
    """
    Retorna os próximos tópicos da disciplina de forma circular.
    Se a disciplina não tiver tópicos, retorna lista vazia.
    """
    if not disciplina["topicos"]:
        return []

    total_topicos = len(disciplina["topicos"])
    topicos_selecionados = []
    for _ in range(quantidade):
        idx = disciplina["indice_topico_atual"] % total_topicos
        topicos_selecionados.append(disciplina["topicos"][idx])
        disciplina["indice_topico_atual"] = (idx + 1) % total_topicos

    return topicos_selecionados


# ---------------------------------------------------------------------------
# Seção 5 — Geração dos blocos de estudo por dia
# ---------------------------------------------------------------------------

def gerar_blocos_do_dia(
    horario_inicio: str,
    horario_fim: str,
    disciplinas: list[dict],
    referencia_revisao: str | None = None,
    revisao_7dias: str | None = None,
    revisao_30dias: bool = False,
) -> list[dict]:
    """
    Gera a sequência de blocos de estudo para um único dia, seguindo o método:
      - 15 min de revisão do dia anterior (24h)
      - Se for dia de revisão de 7 dias: bloco de resolução de questões
      - Se for dia de revisão de 30 dias: bloco de revisão geral
      - Ciclos de 50 min de estudo + 10 min de pausa

    Cada bloco alterna entre sessão de "teoria" e "questões" para a mesma disciplina.
    """
    if not horario_inicio or not horario_fim:
        return []

    cursor_tempo = horario_para_datetime(horario_inicio)
    fim_sessao = horario_para_datetime(horario_fim)

    if fim_sessao <= cursor_tempo:
        fim_sessao += timedelta(days=1)

    blocos = []

    # --- 24h: Revisão do conteúdo do dia anterior ---
    if referencia_revisao:
        fim_revisao = cursor_tempo + timedelta(minutes=DURACAO_REVISAO_DIA_ANTERIOR_MIN)
        blocos.append({
            "inicio": datetime_para_horario(cursor_tempo),
            "fim": datetime_para_horario(fim_revisao),
            "tipo": "revisao_24h",
            "referencia": referencia_revisao,
        })
        cursor_tempo = fim_revisao + timedelta(minutes=5)

    # --- Revisão de 7 dias: foco em resolução de questões ---
    if revisao_7dias:
        fim_rev7 = min(fim_sessao, cursor_tempo + timedelta(minutes=DURACAO_BLOCO_ESTUDO_MIN))
        if fim_rev7 - cursor_tempo >= timedelta(minutes=DURACAO_MINIMA_BLOCO_UTIL_MIN):
            blocos.append({
                "inicio": datetime_para_horario(cursor_tempo),
                "fim": datetime_para_horario(fim_rev7),
                "tipo": "revisao_7dias",
                "referencia": revisao_7dias,
            })
            cursor_tempo = fim_rev7 + timedelta(minutes=DURACAO_PAUSA_MIN)

    # --- Revisão de 30 dias: revisão geral ---
    if revisao_30dias:
        fim_rev30 = min(fim_sessao, cursor_tempo + timedelta(minutes=DURACAO_BLOCO_ESTUDO_MIN))
        if fim_rev30 - cursor_tempo >= timedelta(minutes=DURACAO_MINIMA_BLOCO_UTIL_MIN):
            blocos.append({
                "inicio": datetime_para_horario(cursor_tempo),
                "fim": datetime_para_horario(fim_rev30),
                "tipo": "revisao_30dias",
                "referencia": "Revisão geral de todo o conteúdo estudado",
            })
            cursor_tempo = fim_rev30 + timedelta(minutes=DURACAO_PAUSA_MIN)

    # --- Blocos principais de estudo (método 50/10) ---
    while cursor_tempo + timedelta(minutes=DURACAO_MINIMA_BLOCO_UTIL_MIN) <= fim_sessao:
        fim_bloco = min(fim_sessao, cursor_tempo + timedelta(minutes=DURACAO_BLOCO_ESTUDO_MIN))

        disciplina_atual = selecionar_proxima_disciplina(disciplinas)
        topicos_da_sessao = obter_proximos_topicos(disciplina_atual, quantidade=2)

        tipo_sessao = disciplina_atual["proximo_tipo_sessao"]
        disciplina_atual["proximo_tipo_sessao"] = (
            "questoes" if tipo_sessao == "teoria" else "teoria"
        )

        blocos.append({
            "inicio": datetime_para_horario(cursor_tempo),
            "fim": datetime_para_horario(fim_bloco),
            "disciplina": disciplina_atual["nome"],
            "topicos": topicos_da_sessao,
            "tipo": tipo_sessao,
        })

        cursor_tempo = fim_bloco
        if cursor_tempo + timedelta(minutes=DURACAO_PAUSA_MIN) < fim_sessao:
            cursor_tempo += timedelta(minutes=DURACAO_PAUSA_MIN)
        else:
            break

    return blocos


# ---------------------------------------------------------------------------
# Seção 6 — Montagem do plano de estudos contínuo
# ---------------------------------------------------------------------------

def montar_plano_estudos(conteudos_extraidos: dict, rotina_raw, cargo: str, dificuldade: str = "", data_prova_str: str | None = None) -> dict:
    """
    Orquestra a geração do plano de estudos do dia atual até a data da prova.

    Passos:
    1. Parseia a rotina do usuário.
    2. Monta a lista de disciplinas com pesos.
    3. Para cada dia útil, gera blocos de estudo com método 24/7/30.
    4. Gera blocos contínuos até a data da prova.

    Retorna um dicionário com o plano completo:
    - Cargo, período, lista de dias com blocos e conteúdos extraídos.
    """
    rotina_semanal = parse_rotina_semanal(rotina_raw)
    disciplinas = montar_lista_disciplinas(conteudos_extraidos, dificuldade)

    hoje = datetime.now()
    hoje_subst = hoje.replace(hour=0, minute=0, second=0, microsecond=0)

    # Parse da data da prova
    data_prova = None
    if data_prova_str:
        for fmt in ["%d/%m/%Y", "%d/%m/%y"]:
            try:
                data_prova = datetime.strptime(data_prova_str.strip(), fmt)
                break
            except ValueError:
                continue
    if data_prova is None:
        data_prova = hoje_subst + timedelta(days=60)

    if data_prova <= hoje_subst:
        data_prova = hoje_subst + timedelta(days=60)

    # Normaliza as chaves da rotina para o padrão sem acento
    rotina_sem_acento = {}
    for chave, valor in rotina_semanal.items():
        chave_normalizada = (
            "terca" if chave in ("terca", "terça")
            else "sabado" if chave in ("sabado", "sábado")
            else chave
        )
        rotina_sem_acento[chave_normalizada] = valor

    MapaDiaSemana = {
        "monday": "segunda", "tuesday": "terca", "wednesday": "quarta",
        "thursday": "quinta", "friday": "sexta", "saturday": "sabado",
        "sunday": "domingo",
    }

    dias_do_plano = []
    referencia_conteudo_ontem: str | None = None
    historico_diario: list[str] = []

    data_atual = hoje_subst
    while data_atual <= data_prova:
        dia_semana_en = data_atual.strftime("%A").lower()
        dia_pt = MapaDiaSemana.get(dia_semana_en, dia_semana_en)
        horario_dia = rotina_sem_acento.get(dia_pt)

        dias_desde_inicio = (data_atual - hoje_subst).days

        if horario_dia is None:
            dias_do_plano.append({
                "dia": dia_pt,
                "data": data_atual.strftime("%d/%m/%Y"),
                "folga": True,
                "blocos": [],
            })
            referencia_conteudo_ontem = None
            historico_diario.append("")
            data_atual += timedelta(days=1)
            continue

        # Verifica se há revisão de 7 dias
        revisao_7dias = None
        if dias_desde_inicio >= 7 and dias_desde_inicio % 7 == 0:
            idx_7d = dias_desde_inicio - 7
            if idx_7d < len(historico_diario) and historico_diario[idx_7d]:
                revisao_7dias = f"Questões sobre: {historico_diario[idx_7d]}"

        # Verifica se há revisão de 30 dias
        revisao_30dias = (dias_desde_inicio >= 30 and dias_desde_inicio % 30 == 0)

        blocos = gerar_blocos_do_dia(
            horario_inicio=horario_dia["inicio"],
            horario_fim=horario_dia["fim"],
            disciplinas=disciplinas,
            referencia_revisao=referencia_conteudo_ontem,
            revisao_7dias=revisao_7dias,
            revisao_30dias=revisao_30dias,
        )

        blocos_estudo = [b for b in blocos if b.get("disciplina")]
        if blocos_estudo:
            ultimo_bloco = blocos_estudo[-1]
            topicos_resumidos = ", ".join(ultimo_bloco.get("topicos", [])[:2])
            referencia_conteudo_ontem = f"{ultimo_bloco['disciplina']} — {topicos_resumidos}"
        else:
            referencia_conteudo_ontem = None

        historico_diario.append(referencia_conteudo_ontem or "")

        dias_do_plano.append({
            "dia": dia_pt,
            "data": data_atual.strftime("%d/%m/%Y"),
            "inicio": horario_dia["inicio"],
            "fim": horario_dia["fim"],
            "blocos": blocos,
        })

        data_atual += timedelta(days=1)

    return {
        "cargo": cargo,
        "dias": dias_do_plano,
        "conteudos": conteudos_extraidos,
        "data_inicio": hoje_subst.strftime("%d/%m/%Y"),
        "data_fim": data_prova.strftime("%d/%m/%Y"),
    }


# ---------------------------------------------------------------------------
# Seção 7 — Extração de metadados do edital (via regex)
# ---------------------------------------------------------------------------


def extrair_metadados_edital(texto_edital: str) -> dict:
    """
    Extrai informações básicas do edital usando expressões regulares.

    As informações extraídas são usadas no cabeçalho do PDF gerado.
    Nenhum campo é obrigatório — se não encontrado, o valor fica None.

    Campos extraídos:
    - numero_edital, nome_concurso, salario, banca, vagas, taxa_inscricao,
      data_prova, data_inicio_inscricoes, hora_inicio_inscricoes,
      data_fim_inscricoes, hora_fim_inscricoes, detalhe_vagas
    """
    metadados = {
        "numero_edital": None,
        "nome_concurso": None,
        "salario": None,
        "banca": None,
        "data_inicio_inscricoes": None,
        "hora_inicio_inscricoes": None,
        "data_fim_inscricoes": None,
        "hora_fim_inscricoes": None,
        "taxa_inscricao": None,
        "vagas": None,
        "detalhe_vagas": None,
        "data_prova": None,
    }

    try:
        # Número do edital
        m = re.search(r"Edital\s*(N[ºo]\s*[^\n]+)", texto_edital, re.I)
        if m:
            metadados["numero_edital"] = m.group(1).strip()

        # Nome do concurso (prefeitura)
        m = re.search(r"Prefeitura\s+(Municipal\s+de\s+)?([^\n\-]+)", texto_edital, re.I)
        if m:
            metadados["nome_concurso"] = f"Prefeitura de {m.group(2).strip()}"

        # Salário
        m = re.search(r"Sal[áa]rio\s*R\$\s*([\d\.,]+(?:\s*a\s*R\$\s*[\d\.,]+)?)", texto_edital, re.I)
        if m:
            metadados["salario"] = f"R$ {m.group(1).strip()}"

        # Banca organizadora
        m = re.search(r"Banca\s*[:\-]?\s*([A-ZÇÃÉÍÓÚ\-\. ]{2,})", texto_edital, re.I)
        if m:
            metadados["banca"] = m.group(1).strip()

        # Período de inscrições (início e fim, com horário opcional)
        m = re.search(
            r"Inscri[cç][õo]es?\s*(?:de|do\s*dia)\s*([\d/]{8,10})"
            r"(?:\s*(?:às|a partir das)\s*([\d:]{4,5}))?"
            r"\s*(?:at[eé]\s*|a\s*|\-)\s*([\d/]{8,10})"
            r"(?:\s*(?:às)\s*([\d:]{4,5}))?",
            texto_edital, re.I,
        )
        if m:
            metadados["data_inicio_inscricoes"] = m.group(1).strip()
            metadados["hora_inicio_inscricoes"] = m.group(2).strip() if m.group(2) else None
            metadados["data_fim_inscricoes"] = m.group(3).strip()
            metadados["hora_fim_inscricoes"] = m.group(4).strip() if m.group(4) else None
        else:
            m = re.search(r"Inscri[cç][õo]es?\s*(?:at[eé])\s*([\d/]{8,10})", texto_edital, re.I)
            if m:
                metadados["data_fim_inscricoes"] = m.group(1).strip()

        # Taxa de inscrição
        m = re.search(r"Taxa\s*de\s*inscri[cç][ãa]o\s*R\$\s*([\d\.,]+(?:\s*a\s*R\$\s*[\d\.,]+)?)", texto_edital, re.I)
        if m:
            metadados["taxa_inscricao"] = f"R$ {m.group(1).strip()}"

        # Número de vagas
        m = re.search(r"Vagas?\s*:?\s*(\d{1,5})", texto_edital, re.I)
        if m:
            metadados["vagas"] = m.group(1)

        # Detalhamento de vagas (ampla concorrência, PcD, cotas)
        detalhes_vagas = []
        for descricao_cota in [
            r"ampla concorr[êe]ncia", r"PcD", r"pessoas com defici[êe]ncia",
            r"negros", r"cotas",
        ]:
            m_cota = re.search(rf"(\d+)\s*(?:vagas?\s*)?(?:para\s*)?{descricao_cota}", texto_edital, re.I)
            if m_cota:
                detalhes_vagas.append(f"{m_cota.group(1)} {descricao_cota}")
        if detalhes_vagas:
            metadados["detalhe_vagas"] = "; ".join(detalhes_vagas)

        # Data da prova
        m = re.search(r"Data\s*da\s*prova\s*[:\-]?\s*([\d/]{8,10})", texto_edital, re.I)
        if m:
            metadados["data_prova"] = m.group(1)

    except Exception as erro:
        # Metadados são opcionais; erros não devem interromper o fluxo
        print(f"[AVISO] Falha ao extrair metadados do edital: {erro}")

    return metadados


# ---------------------------------------------------------------------------
# Seção 8 — Geração do PDF do plano de estudos
# ---------------------------------------------------------------------------

def gerar_pdf_plano_estudos(metadados_concurso: dict, plano: dict) -> bytes:
    """
    Gera o PDF do plano de estudos usando ReportLab.

    O PDF contém:
    1. Título com concurso e período
    2. Conteúdos programáticos por disciplina
    3. Cronograma dia a dia com horários e tipos de sessão

    Retorna os bytes do PDF gerado (pronto para envio como arquivo).
    """
    buffer_pdf = io.BytesIO()
    documento = SimpleDocTemplate(
        buffer_pdf,
        pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
    )
    estilos = getSampleStyleSheet()
    elementos_pdf = []

    # --- Título principal ---
    nome_concurso = metadados_concurso.get("nome_concurso") or "Concurso Público"
    numero_edital = metadados_concurso.get("numero_edital") or "Edital"
    data_prova = metadados_concurso.get("data_prova")

    titulo = f"{nome_concurso} — {numero_edital}"
    if data_prova:
        titulo += f" — Prova em {data_prova}"
    elementos_pdf.append(Paragraph(titulo, estilos["Title"]))

    periodo = f"{plano.get('data_inicio', '')} a {plano.get('data_fim', '')}"
    subtitulo = f"Plano de estudo para: {plano.get('cargo', '')} — {periodo}"
    elementos_pdf.append(Paragraph(subtitulo, estilos["Heading3"]))
    elementos_pdf.append(Spacer(1, 6))

    # --- Conteúdos programáticos ---
    conteudos = plano.get("conteudos") or {}
    if conteudos:
        elementos_pdf.append(Paragraph(f"Conteúdos programáticos — {plano.get('cargo')}", estilos["Heading2"]))

        cabecalho_adicionado = False
        secao_atual = None

        for nome_disciplina, dados_disciplina in (conteudos.get("gerais") or {}).items():
            if secao_atual != "gerais":
                if not cabecalho_adicionado:
                    cabecalho_adicionado = True
                secao_atual = "gerais"
            elementos_pdf.append(Paragraph(nome_disciplina, estilos["Heading4"]))
            topicos = dados_disciplina.get("topicos", []) if isinstance(dados_disciplina, dict) else (dados_disciplina or [])
            texto_topicos = "; ".join(str(t).strip().strip(",") for t in topicos if str(t).strip())
            elementos_pdf.append(Paragraph(texto_topicos, estilos["Normal"]))
            elementos_pdf.append(Spacer(1, 2))

        for nome_disciplina, dados_disciplina in (conteudos.get("especificos") or {}).items():
            elementos_pdf.append(Paragraph(nome_disciplina, estilos["Heading4"]))
            topicos = dados_disciplina.get("topicos", []) if isinstance(dados_disciplina, dict) else (dados_disciplina or [])
            texto_topicos = "; ".join(str(t).strip().strip(",") for t in topicos if str(t).strip())
            elementos_pdf.append(Paragraph(texto_topicos, estilos["Normal"]))
            elementos_pdf.append(Spacer(1, 4))

        elementos_pdf.append(Spacer(1, 8))

    # --- Cronograma de estudos ---
    elementos_pdf.append(Paragraph("Cronograma de estudos", estilos["Heading3"]))
    for dia in plano.get("dias", []):
        data_label = dia.get("data", "")
        data_info = f" ({data_label})" if data_label else ""

        if dia.get("folga"):
            elementos_pdf.append(Paragraph(f"{dia['dia'].title()}{data_info}: Folga 🏖️", estilos["Normal"]))
            continue

        elementos_pdf.append(
            Paragraph(
                f"{dia['dia'].title()}{data_info} — {dia.get('inicio')} às {dia.get('fim')}",
                estilos["Heading4"]
            )
        )

        for bloco in dia.get("blocos", []):
            tipo = bloco.get("tipo", "")
            if tipo in ("revisao_24h", "revisao"):
                descricao_bloco = (
                    f"🔁 {bloco['inicio']}–{bloco['fim']}: "
                    f"Revisão 24h — {bloco.get('referencia', '')}"
                )
            elif tipo == "revisao_7dias":
                descricao_bloco = (
                    f"📝 {bloco['inicio']}–{bloco['fim']}: "
                    f"Revisão 7 dias — {bloco.get('referencia', '')}"
                )
            elif tipo == "revisao_30dias":
                descricao_bloco = (
                    f"🔄 {bloco['inicio']}–{bloco['fim']}: "
                    f"Revisão 30 dias — {bloco.get('referencia', '')}"
                )
            else:
                topicos_str = ", ".join(bloco.get("topicos", [])[:2])
                tipo_label = "📖 Teoria" if tipo == "teoria" else "📝 Questões"
                descricao_bloco = (
                    f"{tipo_label}  {bloco['inicio']}–{bloco['fim']}: "
                    f"{bloco.get('disciplina')} — {topicos_str}"
                )
            elementos_pdf.append(Paragraph(descricao_bloco, estilos["Normal"]))

        elementos_pdf.append(Spacer(1, 4))

    documento.build(elementos_pdf)
    buffer_pdf.seek(0)
    return buffer_pdf.read()


# ---------------------------------------------------------------------------
# Seção 9 — Revisão da extração via IA (Reflection Pattern)
# ---------------------------------------------------------------------------

def revisar_conteudos_com_ia(
    conteudos_extraidos: dict,
    cargo: str,
    trecho_edital: str,
) -> dict:
    """
    Segunda passagem de IA para validar e corrigir o JSON de disciplinas.

    Utiliza o padrão "Reflection": um segundo prompt verifica se a extração
    anterior está completa e coerente. Se a IA retornar um JSON válido,
    ele substitui o original. Caso contrário, o original é mantido.

    Parâmetros:
        conteudos_extraidos: JSON gerado na primeira extração.
        cargo: nome do cargo para contextualizar a verificação.
        trecho_edital: trecho do texto do edital (limitado para não exceder tokens).
    """
    prompt_revisao = f"""
Você é um verificador crítico de extrações de edital para o cargo: {cargo}.

Sua tarefa:
1. Verifique se o JSON abaixo contém TODAS as disciplinas e tópicos coerentes com o cargo.
2. Se algo estiver faltando, incorreto ou de outro nível, CORRIJA.
3. Inclua a DATA DA PROVA no campo "data_prova" (formato DD/MM/AAAA) se encontrada no edital.
4. Mantenha o formato EXATO abaixo. Responda SOMENTE com o JSON corrigido.

TRECHO DO EDITAL (contexto):
{trecho_edital[:3500]}

JSON ATUAL:
{json.dumps(conteudos_extraidos, ensure_ascii=False)}

FORMATO ESPERADO (responda apenas com isto):
{{
  "gerais": {{ "Nome da Disciplina": ["tópico 1", "tópico 2"] }},
  "especificos": {{ "Conhecimentos Específicos": ["tópico 1", "tópico 2"] }},
  "data_prova": "DD/MM/AAAA"
}}
"""
    try:
        time.sleep(1)  # Evita rate limit
        resposta = cliente_ia.chat.completions.create(
            model=MODELO_IA,
            messages=[
                {"role": "system", "content": "Você é rigoroso, factual e responde somente com JSON válido."},
                {"role": "user", "content": prompt_revisao},
            ],
            temperature=0.1,
        )
        texto_resposta = resposta.choices[0].message.content.strip()
        texto_resposta = texto_resposta.replace("```json", "").replace("```", "").strip()
        return json.loads(texto_resposta)
    except Exception as erro:
        print(f"[AVISO] Reflection falhou, mantendo extração original. Erro: {erro}")
        return conteudos_extraidos


# ---------------------------------------------------------------------------
# Seção 10 — Endpoints Flask
# ---------------------------------------------------------------------------

@app.route("/extrair_disciplinas", methods=["POST"])
def extrair_disciplinas_do_edital():
    """
    Endpoint leve: extrai APENAS os nomes das disciplinas do edital para um cargo.

    Request: multipart/form-data com:
      - 'edital': arquivo PDF do edital
      - 'cargo': string com o nome do cargo

    Response: JSON { "disciplinas": ["Português", "Matemática", ...] }
    """
    if "edital" not in request.files:
        return jsonify({"erro": "Nenhum arquivo de edital enviado."}), 400

    cargo_escolhido = request.form.get("cargo", "").strip()
    arquivo_edital = request.files["edital"]

    if not cargo_escolhido:
        return jsonify({"erro": "Informe o nome do cargo."}), 400

    texto_edital = ""
    try:
        with fitz.open(stream=arquivo_edital.read(), filetype="pdf") as pdf:
            for pagina in pdf:
                texto_edital += pagina.get_text()
    except Exception as erro:
        return jsonify({"erro": f"Erro ao ler o PDF: {str(erro)}"}), 500

    trecho_para_ia = texto_edital[:6000]

    prompt = f"""
Analise o edital abaixo para o cargo "{cargo_escolhido}".

Extraia APENAS os nomes das disciplinas cobradas, separando em:
- "gerais" (conhecimentos gerais/comuns a vários cargos)
- "especificos" (conhecimentos específicos do cargo)

Responda APENAS com JSON neste formato (sem texto extra):
{{"gerais": ["Disciplina 1", "Disciplina 2"], "especificos": ["Disciplina 3"]}}

TRECHO DO EDITAL:
{trecho_para_ia}
"""
    try:
        resposta = cliente_ia.chat.completions.create(
            model=MODELO_IA,
            messages=[
                {
                    "role": "system",
                    "content": "Você é um especialista em editais. Responda apenas com JSON válido.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )

        texto = resposta.choices[0].message.content.strip()
        texto_limpo = texto.replace("```json", "").replace("```", "").strip()

        # Tenta parse direto
        try:
            dados = json.loads(texto_limpo)
        except Exception:
            # Fallback: extrair entre { }
            inicio = texto_limpo.find("{")
            fim = texto_limpo.rfind("}")
            if inicio != -1 and fim > inicio:
                dados = json.loads(texto_limpo[inicio : fim + 1])
            else:
                raise

        # Achata em uma lista única de nomes
        gerais = dados.get("gerais") or []
        especificos = dados.get("especificos") or []
        todas = (gerais if isinstance(gerais, list) else []) + \
                (especificos if isinstance(especificos, list) else [])

        return jsonify({"disciplinas": todas})

    except Exception as erro:
        return jsonify({"erro": f"Erro ao extrair disciplinas: {str(erro)}"}), 500


@app.route("/teste", methods=["GET"])
def verificar_servidor():
    """Endpoint de health-check. Confirma que o servidor está no ar."""
    return jsonify({"status": "Servidor funcionando!"})


@app.route("/extrair_cargos", methods=["POST"])
def extrair_cargos_do_edital():
    """
    Recebe um PDF de edital e retorna uma lista dos cargos encontrados.

    Uso: o frontend pode chamar este endpoint primeiro para popular
    o campo "Escolha o cargo" antes de chamar /gerar.

    Request: multipart/form-data com campo 'edital' (arquivo PDF).
    Response: JSON { "cargos": ["Cargo A", "Cargo B", ...] }
    """
    if "edital" not in request.files:
        return jsonify({"erro": "Nenhum arquivo de edital enviado."}), 400

    arquivo_edital = request.files["edital"]
    texto_edital = ""
    try:
        with fitz.open(stream=arquivo_edital.read(), filetype="pdf") as pdf:
            for pagina in pdf:
                texto_edital += pagina.get_text()
    except Exception as erro:
        return jsonify({"erro": f"Erro ao ler o PDF: {str(erro)}"}), 500

    # Busca padrões do tipo "Cargo: Analista de Sistemas"
    cargos_encontrados = re.findall(r"Cargo\s*:\s*(.+)", texto_edital)
    cargos_unicos = list(set(c.strip() for c in cargos_encontrados if c.strip()))

    return jsonify({"cargos": cargos_unicos})


@app.route("/gerar", methods=["POST"])
def gerar_plano_de_estudos():
    """
    Endpoint principal: gera e retorna o PDF do plano de estudos.

    Passos:
    1. Valida os dados recebidos (edital PDF, cargo, rotina).
    2. Extrai o texto do PDF.
    3. Chama a IA para extrair as disciplinas e tópicos do edital.
    4. Faz a revisão da extração (Reflection).
    5. Gera os blocos de estudo de forma determinística.
    6. Exporta o plano como PDF e retorna para download.

    Request: multipart/form-data com:
      - 'edital': arquivo PDF do edital
      - 'cargo': string com o nome do cargo
      - 'rotina': JSON string com os horários de estudo por dia

    Response: arquivo PDF para download.
    """
    if "edital" not in request.files:
        return jsonify({"erro": "Nenhum arquivo de edital enviado."}), 400

    rotina_usuario = request.form.get("rotina", "")
    cargo_escolhido = request.form.get("cargo", "")
    dificuldade = request.form.get("dificuldade", "")
    arquivo_edital = request.files["edital"]

    if not rotina_usuario.strip() or not cargo_escolhido.strip():
        return jsonify({"erro": "Dados incompletos: informe o cargo e a rotina."}), 400

    # --- Passo 1: Extrair texto do PDF ---
    texto_edital = ""
    try:
        with fitz.open(stream=arquivo_edital.read(), filetype="pdf") as pdf:
            for pagina in pdf:
                texto_edital += pagina.get_text()
    except Exception as erro:
        return jsonify({"erro": f"Erro ao ler o PDF do edital: {str(erro)}"}), 500

    # Limita o texto para não ultrapassar o contexto da IA
    trecho_para_ia = texto_edital[:6000]

    # --- Passo 2: Prompt de extração de disciplinas ---
    prompt_extracao = f"""
ANÁLISE ESTRUTURAL DO EDITAL — CARGO: "{cargo_escolhido}"

INSTRUÇÕES:
1. Identifique o NÍVEL do cargo (Fundamental, Médio/Técnico ou Superior).
2. Localize a tabela de disciplinas correspondente ao nível identificado.
3. Separe claramente:
   - Disciplinas GERAIS (comuns a vários cargos do mesmo nível)
   - Conhecimentos ESPECÍFICOS (exclusivos do cargo "{cargo_escolhido}")
4. Extraia os tópicos EXATAMENTE como aparecem no edital. NÃO invente tópicos.
5. Localize a DATA DA PROVA no edital e inclua no campo "data_prova" no formato DD/MM/AAAA.

FORMATO OBRIGATÓRIO DE RESPOSTA (apenas o JSON, sem texto adicional):
{{
  "gerais": {{
    "Nome da Disciplina": ["tópico 1", "tópico 2", ...]
  }},
  "especificos": {{
    "Conhecimentos Específicos": ["tópico 1", "tópico 2", ...]
  }},
  "data_prova": "DD/MM/AAAA"
}}

TRECHO DO EDITAL:
{trecho_para_ia}
"""

    try:
        time.sleep(2)  # Evita rate limit

        # --- Passo 3: Chamada à IA para extração ---
        resposta_ia = cliente_ia.chat.completions.create(
            model=MODELO_IA,
            messages=[
                {
                    "role": "system",
                    "content": "Você é um especialista em análise de editais de concurso público. Responda apenas com JSON válido.",
                },
                {"role": "user", "content": prompt_extracao},
            ],
            temperature=0.1,
        )

        texto_resposta_ia = resposta_ia.choices[0].message.content.strip()
        print("[DEBUG] Resposta bruta da IA:", texto_resposta_ia[:500])

        # --- Passo 4: Parse do JSON retornado pela IA ---
        conteudos_extraidos = None
        erros_parse = []

        # Tentativa 1: Parse direto
        try:
            texto_limpo = texto_resposta_ia.replace("```json", "").replace("```", "").strip()
            conteudos_extraidos = json.loads(texto_limpo)
        except Exception as e1:
            erros_parse.append(f"Parse direto falhou: {e1}")

        # Tentativa 2: Extrai apenas a substring entre '{' e '}'
        if conteudos_extraidos is None:
            try:
                pos_inicio = texto_resposta_ia.find("{")
                pos_fim = texto_resposta_ia.rfind("}")
                if pos_inicio != -1 and pos_fim > pos_inicio:
                    substring_json = texto_resposta_ia[pos_inicio : pos_fim + 1]
                    conteudos_extraidos = json.loads(substring_json)
                    print("[DEBUG] Parse alternativo (substring) bem-sucedido.")
                else:
                    erros_parse.append("Nenhum delimitador JSON encontrado na resposta.")
            except Exception as e2:
                erros_parse.append(f"Parse alternativo falhou: {e2}")

        if conteudos_extraidos is None:
            print("[ERRO] Todos os métodos de parse falharam:", erros_parse)
            return make_response(
                jsonify({
                    "erro": "Não foi possível interpretar a resposta da IA.",
                    "detalhes": erros_parse,
                    "resposta_bruta": texto_resposta_ia[:2000],
                }),
                500,
            )

        # --- Passo 5: Revisão via Reflection ---
        conteudos_extraidos = revisar_conteudos_com_ia(conteudos_extraidos, cargo_escolhido, trecho_para_ia)

        # Valida se ao menos uma disciplina foi extraída
        disciplinas_teste = montar_lista_disciplinas(conteudos_extraidos)
        if not disciplinas_teste:
            return make_response(
                jsonify({
                    "erro": "Nenhuma disciplina foi extraída do edital. Verifique o PDF ou o cargo informado.",
                    "resposta_bruta": texto_resposta_ia[:2000],
                }),
                500,
            )

        # --- Passo 6: Extrai metadados e data da prova ---
        metadados_concurso = extrair_metadados_edital(texto_edital)
        data_prova = metadados_concurso.get("data_prova") or conteudos_extraidos.get("data_prova")

        # --- Passo 7: Montagem do plano contínuo e geração do PDF ---
        plano_estudos = montar_plano_estudos(conteudos_extraidos, rotina_usuario, cargo_escolhido, dificuldade, data_prova)
        bytes_pdf = gerar_pdf_plano_estudos(metadados_concurso, plano_estudos)

        nome_arquivo = f"plano_{cargo_escolhido.replace(' ', '_')}.pdf"
        return send_file(
            io.BytesIO(bytes_pdf),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=nome_arquivo,
        )

    except Exception as erro_geral:
        print(f"[ERRO] Falha geral no endpoint /gerar: {erro_geral}")
        return jsonify({"erro": f"Erro interno: {str(erro_geral)}"}), 500


@app.route("/informar", methods=["POST"])
def fluxo_interativo_informar():
    """
    Endpoint de planejamento interativo (sem edital em PDF).

    Permite ao usuário construir um plano respondendo perguntas passo a passo:
    1. Quais disciplinas para o cargo?
    2. Qual a banca do concurso?
    3. Qual método de estudo? (ex.: 50/10, Pomodoro, SRS)
    4. Como avaliar a eficiência do plano?

    Ao final, a IA revisa as respostas e retorna um plano-resumo estruturado.

    Request JSON:
    {
      "session_id": "uuid" (ausente na primeira chamada),
      "answer": "resposta à pergunta anterior",
      "rotina": "JSON string da rotina (opcional)",
      "cargo": "nome do cargo (opcional)"
    }

    Response:
    - Durante o fluxo: { "session_id": "...", "next_question": "...", "partial": {...} }
    - Ao final: { "resultado": {...}, "message": "Planejamento concluído." }
    """
    try:
        dados_request = request.get_json(force=True, silent=True) or {}
        session_id = dados_request.get("session_id")
        resposta_usuario = dados_request.get("answer")
        rotina_usuario = dados_request.get("rotina", "")
        cargo_usuario = dados_request.get("cargo", "")

        # Sequência de perguntas do fluxo interativo
        perguntas_fluxo = [
            ("disciplinas", "1) Quais são as disciplinas cobradas para o cargo? Liste-as."),
            ("banca", "2) Qual é a banca organizadora do concurso?"),
            ("metodo_estudo", "3) Qual método de estudo você prefere? (ex.: 50/10, Pomodoro 25/5, Revisão Espaçada)"),
            ("criterio_avaliacao", "4) Como você quer avaliar sua evolução? (ex.: nº de questões/semana, % de acertos, simulados)"),
        ]

        # Primeira chamada: cria nova sessão e retorna a primeira pergunta
        if not session_id:
            session_id = str(uuid.uuid4())
            sessoes_ativas[session_id] = {
                "etapa_atual": 0,
                "respostas_usuario": {},
                "rotina": rotina_usuario,
                "cargo": cargo_usuario,
            }
            _, primeira_pergunta = perguntas_fluxo[0]
            return jsonify({"session_id": session_id, "next_question": primeira_pergunta})

        # Recupera sessão existente
        sessao = sessoes_ativas.get(session_id)
        if not sessao:
            return jsonify({"erro": "Sessão não encontrada. Reinicie o fluxo."}), 400

        # Salva a resposta da etapa atual e avança para a próxima
        etapa_atual = sessao["etapa_atual"]
        if etapa_atual < len(perguntas_fluxo) and resposta_usuario is not None:
            chave_resposta, _ = perguntas_fluxo[etapa_atual]
            sessao["respostas_usuario"][chave_resposta] = resposta_usuario
            sessao["etapa_atual"] = etapa_atual + 1

        # Verifica se ainda há perguntas
        etapa_atual = sessao["etapa_atual"]
        if etapa_atual < len(perguntas_fluxo):
            _, proxima_pergunta = perguntas_fluxo[etapa_atual]
            return jsonify({
                "session_id": session_id,
                "next_question": proxima_pergunta,
                "respostas_parciais": sessao["respostas_usuario"],
            })

        # Todas as perguntas respondidas: monta o plano-resumo
        respostas = sessao["respostas_usuario"]
        plano_interativo = {
            "cargo": sessao.get("cargo"),
            "rotina": sessao.get("rotina"),
            "disciplinas_informadas": respostas.get("disciplinas"),
            "banca": respostas.get("banca"),
            "metodo_estudo": respostas.get("metodo_estudo"),
            "criterio_avaliacao": respostas.get("criterio_avaliacao"),
            "observacoes": "Plano criado via fluxo interativo. Ajuste conforme necessário.",
        }

        # Revisão final com IA
        prompt_revisao_final = f"""
Verifique se o plano abaixo está completo e coerente para o cargo: {sessao.get('cargo')}.
Se algo importante estiver faltando, adicione no campo 'observacoes'.
Responda SOMENTE com o JSON, sem texto adicional.

{json.dumps(plano_interativo, ensure_ascii=False)}
"""
        try:
            time.sleep(1)
            resposta_revisao = cliente_ia.chat.completions.create(
                model=MODELO_IA,
                messages=[
                    {"role": "system", "content": "Você é um revisor sucinto que responde apenas com JSON válido."},
                    {"role": "user", "content": prompt_revisao_final},
                ],
                temperature=0.1,
            )
            texto_revisao = resposta_revisao.choices[0].message.content.strip()
            plano_interativo = json.loads(
                texto_revisao.replace("```json", "").replace("```", "").strip()
            )
        except Exception as erro_revisao:
            print(f"[AVISO] Revisão final do fluxo interativo falhou: {erro_revisao}")

        # Encerra e limpa a sessão
        sessoes_ativas.pop(session_id, None)

        return jsonify({
            "resultado": plano_interativo,
            "message": "Planejamento concluído com sucesso!",
        })

    except Exception as erro_geral:
        print(f"[ERRO] Falha no endpoint /informar: {erro_geral}")
        return jsonify({"erro": f"Erro no fluxo interativo: {str(erro_geral)}"}), 500


# ---------------------------------------------------------------------------
# Inicialização do servidor
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    print(f"Iniciando servidor Flask na porta {porta}...")
    app.run(host="0.0.0.0", port=porta, debug=True)