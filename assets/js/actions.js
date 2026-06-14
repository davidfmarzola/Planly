const gerarBtn = document.getElementById('gerar');
const informarBtn = document.getElementById('informar');
const inputCargo = document.getElementById('cargo');
const loader = document.getElementById('loading');
const editalInput = document.getElementById('edital');
const mostrarConteudoBtn = document.getElementById('mostrarConteudo');
const conteudoDisciplinas = document.getElementById('conteudo-disciplinas');
const listaDisciplinas = document.getElementById('lista-disciplinas');
const dificuldadeInput = document.getElementById('dificuldade');
const BASE_URL = 'https://planly-qmpv.onrender.com';

function getRotinaJSON() {
  const radio = document.querySelector('input[name="horas_estudo"]:checked');
  const horas = radio ? parseInt(radio.value, 10) : 2;

  const mapeamento = {
    1: { inicio: "19:00", fim: "20:00" },
    2: { inicio: "19:00", fim: "21:00" },
    3: { inicio: "18:00", fim: "21:00" },
    4: { inicio: "18:00", fim: "22:00" },
    5: { inicio: "17:00", fim: "22:00" },
  };

  const slot = mapeamento[horas] || mapeamento[2];

  return JSON.stringify({
    segunda: { ...slot },
    terca:   { ...slot },
    quarta:  { ...slot },
    quinta:  { ...slot },
    sexta:   { ...slot },
    sabado:  { ...slot },
    domingo: { ...slot },
  });
}

async function enviarDados(url) {
  const arquivo = editalInput.files[0];

  if (!inputCargo.value.trim()) {
    alert('Preencha o cargo antes de gerar o plano.');
    return;
  }

  let dados;
  let isArquivo = false;

  if (arquivo) {
    dados = new FormData();
    dados.append('rotina', getRotinaJSON());
    dados.append('cargo', inputCargo.value);
    dados.append('edital', arquivo);
    dados.append('dificuldade', dificuldadeInput.value.trim());
    isArquivo = true;
  } else {
    dados = {
      rotina: getRotinaJSON(),
      cargo: inputCargo.value,
      dificuldade: dificuldadeInput.value.trim(),
    };
  }

  mostrarLoader(true);

  try {
    const resposta = await fetch(url, {
      method: 'POST',
      body: isArquivo ? dados : JSON.stringify(dados),
      headers: isArquivo ? undefined : { 'Content-Type': 'application/json' }
    });

    if (!resposta.ok) {
      try {
        const erroJson = await resposta.json();
        alert('Erro: ' + (erroJson.erro || 'Erro desconhecido'));
      } catch (_) {
        alert('Erro ao processar requisição.');
      }
      return;
    }

    const ct = resposta.headers.get('Content-Type') || '';
    if (ct.includes('application/pdf')) {
      const blob = await resposta.blob();
      const urlBlob = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = urlBlob;
      a.download = 'plano_estudos.pdf';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(urlBlob);
      exibirModal('PDF gerado e baixado.');
    } else {
      const json = await resposta.json();
      exibirModal(json.resultado || json.erro || json);
    }

  } catch (err) {
    alert('Erro ao conectar com o servidor: ' + err.message);
  } finally {
    mostrarLoader(false);
  }
}

function mostrarLoader(exibir) {
  loader.style.display = exibir ? 'flex' : 'none';
}

function exibirModal(conteudo) {
  const modalAntigo = document.getElementById('modalResultado');
  if (modalAntigo) modalAntigo.remove();

  const modal = document.createElement('div');
  modal.id = 'modalResultado';
  modal.className = 'modal-resultado';
  const display = (typeof conteudo === 'object') ? JSON.stringify(conteudo, null, 2) : String(conteudo);
  modal.innerHTML = `
    <div class="modal-content">
      <button class="fechar-btn" title="Fechar resultado">&times;</button>
      <h2>Seu plano está pronto!</h2>
      <pre>${display}</pre>
    </div>
  `;

  document.body.appendChild(modal);
  modal.scrollIntoView({ behavior: 'smooth' });

  modal.querySelector('.fechar-btn').addEventListener('click', () => modal.remove());
  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.remove();
  });
}

editalInput.addEventListener("change", async function () {
  const arquivo = this.files[0];
  if (!arquivo) return;

  const dados = new FormData();
  dados.append("edital", arquivo);

  try {
    const resposta = await fetch(`${BASE_URL}/extrair_cargos`, {
      method: "POST",
      body: dados
    });

    if (!resposta.ok) {
      throw new Error("Erro ao processar edital");
    }

    const resultado = await resposta.json();
    const lista = document.getElementById("opcoes");
    lista.innerHTML = "";

    resultado.cargos.forEach(cargo => {
      const option = document.createElement("option");
      option.value = cargo;
      lista.appendChild(option);
    });
  } catch (e) {
    console.error("Erro:", e);
    alert("Erro ao extrair cargos do edital.");
  }
});

// --- Mostrar Conteúdo ---
mostrarConteudoBtn.addEventListener('click', async function (e) {
  e.preventDefault();

  const arquivo = editalInput.files[0];
  if (!arquivo) {
    alert('Selecione o edital (PDF) primeiro.');
    return;
  }
  if (!inputCargo.value.trim()) {
    alert('Digite o nome do cargo.');
    return;
  }

  mostrarLoader(true);

  try {
    const dados = new FormData();
    dados.append('edital', arquivo);
    dados.append('cargo', inputCargo.value.trim());

    const resposta = await fetch(`${BASE_URL}/extrair_disciplinas`, {
      method: 'POST',
      body: dados,
    });

    if (!resposta.ok) {
      const erro = await resposta.json();
      alert('Erro: ' + (erro.erro || 'Erro ao extrair disciplinas.'));
      return;
    }

    const json = await resposta.json();
    const disciplinas = json.disciplinas || [];

    // Renderiza cada disciplina como uma tag
    listaDisciplinas.innerHTML = disciplinas.map(nome =>
      `<span class="disciplina-tag">${nome}</span>`
    ).join('');

    // Mostra o container e esconde o botão
    conteudoDisciplinas.style.display = 'flex';
    mostrarConteudoBtn.style.display = 'none';

    // Rola para o container
    conteudoDisciplinas.scrollIntoView({ behavior: 'smooth' });

  } catch (err) {
    alert('Erro ao conectar com o servidor: ' + err.message);
  } finally {
    mostrarLoader(false);
  }
});

// --- GERAR ---
gerarBtn.addEventListener('click', (e) => {
  e.preventDefault();
  enviarDados(`${BASE_URL}/gerar`);
});

// Fluxo interativo Planning/ReAct
informarBtn.addEventListener('click', async (e) => {
  e.preventDefault();
  mostrarLoader(true);
  try {
    const iniciar = await fetch(`${BASE_URL}/informar`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rotina: getRotinaJSON(), cargo: inputCargo.value })
    });
    const initJson = await iniciar.json();
    if (initJson.erro) throw new Error(initJson.erro);

    let sessionId = initJson.session_id;
    let pergunta = initJson.next_question;
    let partial = initJson.partial || {};

    while (pergunta) {
      const respostaUsuario = prompt(pergunta);
      if (respostaUsuario === null) {
        alert('Fluxo cancelado.');
        return;
      }
      const r = await fetch(`${BASE_URL}/informar`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, answer: respostaUsuario })
      });
      const j = await r.json();
      if (j.resultado) {
        exibirModal(j.resultado);
        return;
      }
      if (j.erro) throw new Error(j.erro);
      sessionId = j.session_id || sessionId;
      pergunta = j.next_question;
      partial = j.partial || partial;
    }
  } catch (err) {
    alert('Erro no fluxo interativo: ' + err.message);
  } finally {
    mostrarLoader(false);
  }
});