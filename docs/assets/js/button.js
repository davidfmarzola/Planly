document.addEventListener('DOMContentLoaded', function () {
  const saibaMais = document.getElementById('saibaMais');
  if (saibaMais) {
    saibaMais.addEventListener('click', function () {
      document.getElementById('formulario').scrollIntoView({
        behavior: 'smooth'
      });
    });
  }

  const gerarExemplo = document.getElementById('gerarExemplo');
  if (gerarExemplo) {
    gerarExemplo.addEventListener('click', function () {
      const formSection = document.getElementById('formulario');
      formSection.scrollIntoView({ behavior: 'smooth' });
    });
  }
});