const BASE_URL = 'https://planly-qmpv.onrender.com';

function fetchComTimeout(url, options = {}, timeoutMs = 60000) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...options, signal: controller.signal }).finally(() => clearTimeout(id));
}

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

async function enviarDados(url, inputCargo, editalInput, dificuldadeInput) {
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

  const loader = document.getElementById('loading');
  mostrarLoader(true, 'Montando plano de estudos...', loader);

  try {
    const resposta = await fetchComTimeout(url, {
      method: 'POST',
      body: isArquivo ? dados : JSON.stringify(dados),
      headers: isArquivo ? undefined : { 'Content-Type': 'application/json' }
    }, 60000);

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
    mostrarLoader(false, '', loader);
  }
}

function mostrarLoader(exibir, mensagem, loader) {
  if (!loader) return;
  loader.style.display = exibir ? 'flex' : 'none';
  if (exibir && mensagem) {
    document.getElementById('loading-text').textContent = mensagem;
  }
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

document.addEventListener('DOMContentLoaded', function () {
  const editalInput = document.getElementById('edital');
  const inputCargo = document.getElementById('cargo');
  const mostrarConteudoBtn = document.getElementById('mostrarConteudo');
  const conteudoDisciplinas = document.getElementById('conteudo-disciplinas');
  const listaDisciplinas = document.getElementById('lista-disciplinas');
  const dificuldadeInput = document.getElementById('dificuldade');
  const gerarBtn = document.getElementById('gerar');
  const informarBtn = document.getElementById('informar');

  if (!editalInput || !inputCargo) return;

  editalInput.addEventListener('change', async function () {
    const arquivo = this.files[0];
    if (!arquivo) return;

    const dados = new FormData();
    dados.append('edital', arquivo);

    try {
      const resposta = await fetchComTimeout(`${BASE_URL}/extrair_cargos`, {
        method: 'POST',
        body: dados
      }, 30000);

      if (!resposta.ok) {
        throw new Error('Erro ao processar edital');
      }

      const resultado = await resposta.json();
      const lista = document.getElementById('opcoes');
      lista.innerHTML = '';

      (resultado.cargos || []).forEach(cargo => {
        const option = document.createElement('option');
        option.value = cargo;
        lista.appendChild(option);
      });
    } catch (e) {
      console.error('Erro:', e);
      alert('Erro ao extrair cargos do edital.');
    }
  });

  if (mostrarConteudoBtn) {
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

      const loader = document.getElementById('loading');
      mostrarLoader(true, 'Analisando edital...', loader);

      try {
        const dados = new FormData();
        dados.append('edital', arquivo);
        dados.append('cargo', inputCargo.value.trim());

        const resposta = await fetchComTimeout(`${BASE_URL}/extrair_disciplinas`, {
          method: 'POST',
          body: dados,
        }, 120000);

        if (!resposta.ok) {
          const erro = await resposta.json();
          alert('Erro: ' + (erro.erro || 'Erro ao extrair disciplinas.'));
          return;
        }

        const json = await resposta.json();
        const disciplinas = json.disciplinas || [];

        listaDisciplinas.innerHTML = disciplinas.map(nome =>
          `<span class="disciplina-tag">${nome}</span>`
        ).join('');

        conteudoDisciplinas.style.display = 'flex';
        mostrarConteudoBtn.style.display = 'none';

        conteudoDisciplinas.scrollIntoView({ behavior: 'smooth' });

      } catch (err) {
        alert('Erro ao conectar com o servidor: ' + err.message);
      } finally {
        mostrarLoader(false, '', loader);
      }
    });
  }

  if (gerarBtn) {
    gerarBtn.addEventListener('click', (e) => {
      e.preventDefault();
      enviarDados(`${BASE_URL}/gerar`, inputCargo, editalInput, dificuldadeInput);
    });
  }

  if (informarBtn) {
    informarBtn.addEventListener('click', async (e) => {
      e.preventDefault();
      const loader = document.getElementById('loading');
      mostrarLoader(true, '', loader);
      try {
        const iniciar = await fetchComTimeout(`${BASE_URL}/informar`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rotina: getRotinaJSON(), cargo: inputCargo.value })
        }, 30000);
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
          const r = await fetchComTimeout(`${BASE_URL}/informar`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId, answer: respostaUsuario })
          }, 30000);
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
        mostrarLoader(false, '', loader);
      }
    });
  }
});