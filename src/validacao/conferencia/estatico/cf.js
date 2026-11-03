(function () {
  "use strict";
  const painel = document.getElementById("painel");
  const fundo = document.getElementById("painel-fundo");
  const corpo = painel ? painel.querySelector(".painel-corpo") : null;
  let casoAberto = null;

  function idsDaPagina() {
    return Array.from(document.querySelectorAll("tr[data-caso]")).map((tr) => tr.dataset.caso);
  }

  function marcarLinha(id) {
    document.querySelectorAll("tr.aberta").forEach((tr) => tr.classList.remove("aberta"));
    const tr = document.querySelector(`tr[data-caso="${id}"]`);
    if (tr) tr.classList.add("aberta");
  }

  async function abrirCaso(id, manterRolagem) {
    if (!painel) { window.location = `/caso/${id}`; return; }
    casoAberto = String(id);
    painel.classList.add("aberto");
    fundo.classList.add("aberto");
    corpo.classList.add("carregando");
    const topo = corpo.scrollTop;
    try {
      const resposta = await fetch(`/caso/${id}?parcial=1`, { headers: { "X-Parcial": "1" } });
      if (resposta.redirected) {
        const novo = resposta.url.match(/\/caso\/(\d+)/);
        if (novo) casoAberto = novo[1];
      }
      corpo.innerHTML = await resposta.text();
      if (manterRolagem) corpo.scrollTop = topo; else corpo.scrollTop = 0;
      prepararFormulario();
    } catch (erro) {
      corpo.innerHTML = `<div class="erro">Não foi possível abrir o caso: ${erro}</div>`;
    } finally {
      corpo.classList.remove("carregando");
    }
    marcarLinha(casoAberto);
    const url = new URL(window.location);
    url.searchParams.set("caso", casoAberto);
    history.replaceState(null, "", url);
    atualizarNavegacao();
  }

  function fecharCaso() {
    if (!painel) return;
    painel.classList.remove("aberto");
    fundo.classList.remove("aberto");
    const url = new URL(window.location);
    url.searchParams.delete("caso");
    history.replaceState(null, "", url);
    const tr = document.querySelector(`tr[data-caso="${casoAberto}"]`);
    casoAberto = null;
    if (tr) { tr.querySelector("[data-analisar]")?.focus(); }
  }

  function atualizarNavegacao() {
    const ids = idsDaPagina();
    const pos = ids.indexOf(casoAberto);
    const ant = document.getElementById("caso-anterior");
    const prox = document.getElementById("caso-proximo");
    const posicao = document.getElementById("caso-posicao");
    if (ant) ant.disabled = pos <= 0;
    if (prox) prox.disabled = pos < 0 || pos >= ids.length - 1;
    if (posicao) posicao.textContent = pos >= 0 ? `${pos + 1} de ${ids.length} nesta página` : "";
  }

  function navegar(passo) {
    const ids = idsDaPagina();
    const pos = ids.indexOf(casoAberto);
    const alvo = ids[pos + passo];
    if (alvo) abrirCaso(alvo);
  }

  async function atualizarLinha(id) {
    const tr = document.querySelector(`tr[data-caso="${id}"]`);
    if (!tr) return;
    const resposta = await fetch(`/caso/${id}/linha${window.location.search}`);
    if (!resposta.ok) return;
    const modelo = document.createElement("tbody");
    modelo.innerHTML = (await resposta.text()).trim();
    const nova = modelo.querySelector("tr");
    if (nova) { tr.replaceWith(nova); marcarLinha(id); }
  }

  function prepararFormulario() {
    const form = corpo && corpo.querySelector("form.decisao-form");
    if (!form) return;
    const atualizarExtras = () => {
      const acao = form.querySelector("input[name=acao]:checked")?.value || "";
      form.querySelectorAll(".extra").forEach((el) => {
        const lista = (el.dataset.acoes || "").split(",");
        el.classList.toggle("visivel", lista.includes(acao));
      });
      const just = form.querySelector("[data-justificativa-obrigatoria]");
      if (just) {
        const exige = (just.dataset.justificativaObrigatoria || "").split(",").includes(acao);
        just.textContent = exige ? "(obrigatória)" : "(opcional)";
      }
    };
    form.addEventListener("change", atualizarExtras);
    atualizarExtras();
    form.addEventListener("submit", async (evento) => {
      evento.preventDefault();
      const mensagem = form.querySelector(".form-mensagem");
      const botao = form.querySelector("button[type=submit]");
      botao.disabled = true;
      mensagem.className = "form-mensagem";
      mensagem.textContent = "Registrando...";
      try {
        const resposta = await fetch(form.action, { method: "POST", body: new FormData(form) });
        const dados = await resposta.json();
        if (!dados.ok) {
          mensagem.className = "form-mensagem erro";
          mensagem.textContent = dados.erro || "Não foi possível registrar.";
          botao.disabled = false;
          return;
        }
        const id = form.dataset.caso;
        if (painel) {
          await abrirCaso(id, true);
          const novaMsg = corpo.querySelector(".form-mensagem");
          if (novaMsg) { novaMsg.className = "form-mensagem aviso"; novaMsg.textContent = dados.mensagem; }
          atualizarLinha(id);
        } else {
          window.location.reload();
        }
      } catch (erro) {
        mensagem.className = "form-mensagem erro";
        mensagem.textContent = String(erro);
        botao.disabled = false;
      }
    });
  }

  async function alternarDetalhe(botao) {
    const tr = botao.closest("tr");
    const id = tr.dataset.caso;
    const existente = tr.nextElementSibling;
    if (existente && existente.classList.contains("linha-detalhe")) {
      existente.remove();
      botao.setAttribute("aria-expanded", "false");
      botao.textContent = "▸";
      return;
    }
    const detalhe = document.createElement("tr");
    detalhe.className = "linha-detalhe";
    const celula = document.createElement("td");
    celula.colSpan = tr.children.length;
    celula.innerHTML = '<div class="detalhe sutil">Carregando registros…</div>';
    detalhe.appendChild(celula);
    tr.after(detalhe);
    botao.setAttribute("aria-expanded", "true");
    botao.textContent = "▾";
    const resposta = await fetch(`/caso/${id}/registros`);
    celula.innerHTML = await resposta.text();
  }

  document.addEventListener("click", (evento) => {
    const analisar = evento.target.closest("[data-analisar]");
    if (analisar) { evento.preventDefault(); abrirCaso(analisar.dataset.analisar); return; }
    const exportar = evento.target.closest("[data-exportar]");
    if (exportar) { evento.preventDefault(); exportarEmSegundoPlano(exportar); return; }
    const composicao = evento.target.closest("[data-composicao]");
    if (composicao) {
      evento.preventDefault();
      const linha = document.getElementById(composicao.dataset.composicao);
      if (linha) {
        linha.hidden = !linha.hidden;
        composicao.setAttribute("aria-expanded", String(!linha.hidden));
        composicao.textContent = linha.hidden ? "▸ composição" : "▾ composição";
      }
      return;
    }
    const expandir = evento.target.closest("[data-expandir]");
    if (expandir) { evento.preventDefault(); alternarDetalhe(expandir); return; }
    if (evento.target.closest("[data-fechar]") || evento.target === fundo) { fecharCaso(); return; }
    if (evento.target.closest("#caso-anterior")) { navegar(-1); return; }
    if (evento.target.closest("#caso-proximo")) { navegar(1); return; }
    const validar = evento.target.closest("[data-validar]");
    if (validar) { evento.preventDefault(); iniciarValidacao(); }
    const pacote = evento.target.closest("[data-importar-siconfi]");
    if (pacote) {
      evento.preventDefault(); pacote.disabled = true;
      const quadro = document.querySelector("[data-progresso]");
      if (quadro) { quadro.hidden = false; quadro.scrollIntoView({ block: "nearest" }); }
      iniciarValidacao("/importar/siconfi");
    }
  });

  document.addEventListener("keydown", (evento) => {
    if (!casoAberto) return;
    const digitando = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
    if (evento.key === "Escape") fecharCaso();
    if (!digitando && evento.altKey && evento.key === "ArrowDown") { evento.preventDefault(); navegar(1); }
    if (!digitando && evento.altKey && evento.key === "ArrowUp") { evento.preventDefault(); navegar(-1); }
  });

  async function exportarEmSegundoPlano(botao) {
    const alvo = document.querySelector("[data-exportacao-estado]");
    const dados = new FormData();
    dados.append("formato", botao.dataset.exportar);
    dados.append("query", botao.dataset.query || "");
    botao.setAttribute("aria-disabled", "true");
    if (alvo) alvo.textContent = "Preparando o arquivo…";
    try {
      const resposta = await fetch("/resultado/exportar/iniciar", { method: "POST", body: dados });
      const inicio = await resposta.json();
      if (!inicio.ok) throw new Error(inicio.erro || "Falha ao iniciar.");
      for (;;) {
        await new Promise((r) => setTimeout(r, 1000));
        const estado = await (await fetch(`/tarefas/${inicio.tarefa}`)).json();
        if (estado.erro) throw new Error(estado.erro);
        if (alvo) alvo.textContent = estado.mensagem || "Preparando o arquivo…";
        if (estado.pronto) {
          if (alvo) alvo.textContent = `Pronto: ${estado.nome_arquivo}`;
          window.location = `/tarefas/${inicio.tarefa}/arquivo`;
          break;
        }
        if (!estado.rodando) throw new Error("A exportação terminou sem arquivo.");
      }
    } catch (erro) {
      if (alvo) alvo.textContent = `Falha na exportação: ${erro.message}`;
    } finally {
      botao.removeAttribute("aria-disabled");
    }
  }

  let quadroAtivo = null;
  document.addEventListener("submit", async (evento) => {
    const form = evento.target.closest("form[data-importar-bases]");
    if (!form) return;
    evento.preventDefault();
    const botao = form.querySelector("button[type=submit]");
    const mensagem = form.querySelector("[data-bases-mensagem]");
    botao.disabled = true;
    if (mensagem) mensagem.textContent = "Enviando…";
    try {
      const resposta = await fetch("/importar/bases", { method: "POST", body: new FormData(form) });
      const dados = await resposta.json();
      if (!dados.ok) { if (mensagem) mensagem.textContent = dados.erro; botao.disabled = false; return; }
      if (mensagem) mensagem.textContent = "Reconhecidas: " + dados.reconhecidos.join(", ") + ".";
      quadroAtivo = document.querySelector("[data-progresso-bases]");
      if (quadroAtivo) { quadroAtivo.hidden = false; quadroAtivo.scrollIntoView({ block: "nearest" }); }
      acompanhar();
    } catch (erro) {
      if (mensagem) mensagem.textContent = "Falha no envio: " + erro;
      botao.disabled = false;
    }
  });

  async function iniciarValidacao(rota) {
    const alvo = document.getElementById("estado-validacao");
    const resposta = await fetch(rota || "/validar", { method: "POST" });
    const dados = await resposta.json();
    if (!dados.ok && alvo) { alvo.textContent = dados.erro; return; }
    acompanhar();
  }

  async function acompanhar() {
    const alvo = document.getElementById("estado-validacao");
    const resposta = await fetch("/validar/estado");
    const estado = await resposta.json();
    if (alvo) alvo.textContent = estado.rodando ? `Validando: ${estado.mensagem}` : (estado.erro ? `Falha: ${estado.erro}` : estado.mensagem);
    const quadro = quadroAtivo || document.querySelector(estado.tipo === "importar_bases" ? "[data-progresso-bases]" : "[data-progresso]");
    if (quadro && (estado.rodando || estado.erro)) {
      quadro.hidden = false;
      quadro.classList.toggle("falhou", !!estado.erro && !estado.rodando);
      const texto = quadro.querySelector("[data-progresso-texto]");
      if (texto) texto.textContent = estado.rodando ? estado.mensagem : `Falhou: ${estado.erro}`;
    }
    if (estado.rodando) { setTimeout(acompanhar, 1200); return; }
    if (!estado.erro && (estado.execucao || estado.tipo === "importar_bases")) setTimeout(() => window.location.reload(), 600);
  }

  const inicial = new URL(window.location).searchParams.get("caso");
  if (inicial && painel) abrirCaso(inicial);
  if (document.body.dataset.validando === "1") acompanhar();
  prepararFormularioPagina();

  function prepararFormularioPagina() {
    if (painel) return;
    const form = document.querySelector(".caso-pagina form.decisao-form");
    if (!form) return;
    const atualizarExtras = () => {
      const acao = form.querySelector("input[name=acao]:checked")?.value || "";
      form.querySelectorAll(".extra").forEach((el) => el.classList.toggle("visivel", (el.dataset.acoes || "").split(",").includes(acao)));
      const just = form.querySelector("[data-justificativa-obrigatoria]");
      if (just) just.textContent = (just.dataset.justificativaObrigatoria || "").split(",").includes(acao) ? "(obrigatória)" : "(opcional)";
    };
    form.addEventListener("change", atualizarExtras);
    atualizarExtras();
    form.addEventListener("submit", async (evento) => {
      evento.preventDefault();
      const mensagem = form.querySelector(".form-mensagem");
      const resposta = await fetch(form.action, { method: "POST", body: new FormData(form) });
      const dados = await resposta.json();
      if (dados.ok) { window.location.reload(); return; }
      mensagem.className = "form-mensagem erro";
      mensagem.textContent = dados.erro;
    });
  }
})();

/* Autocompletar de ente (nome ou código IBGE) nos filtros */
(function () {
  "use strict";
  const campos = document.querySelectorAll("input[data-autocompletar-ente]");
  if (!campos.length) return;
  let contador = 0;

  function escapar(texto) {
    return String(texto).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function semAcento(texto) {
    return texto.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }
  function destacar(nome, termo) {
    if (!termo || /^\d+$/.test(termo)) return escapar(nome);
    const base = semAcento(nome);
    const i = base.indexOf(semAcento(termo));
    if (i < 0) return escapar(nome);
    return escapar(nome.slice(0, i)) + "<mark>" + escapar(nome.slice(i, i + termo.length)) + "</mark>" + escapar(nome.slice(i + termo.length));
  }

  campos.forEach((campo) => {
    contador += 1;
    const idLista = `ac-entes-${contador}`;
    const caixa = campo.closest(".campo") || campo.parentElement;
    caixa.classList.add("ac-caixa");
    const lista = document.createElement("ul");
    lista.id = idLista;
    lista.className = "ac-lista";
    lista.setAttribute("role", "listbox");
    lista.hidden = true;
    caixa.appendChild(lista);
    const dica = document.createElement("span");
    dica.className = "ac-dica";
    caixa.appendChild(dica);
    campo.setAttribute("role", "combobox");
    campo.setAttribute("aria-autocomplete", "list");
    campo.setAttribute("aria-controls", idLista);
    campo.setAttribute("aria-expanded", "false");

    let itens = [];
    let ativo = -1;
    let espera = null;
    let ultimaBusca = "";

    function fechar() {
      lista.hidden = true;
      campo.setAttribute("aria-expanded", "false");
      campo.removeAttribute("aria-activedescendant");
      ativo = -1;
    }
    function marcar(indice) {
      ativo = indice;
      lista.querySelectorAll("li").forEach((li, i) => li.setAttribute("aria-selected", String(i === indice)));
      const li = lista.children[indice];
      if (li) { campo.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({ block: "nearest" }); }
    }
    function escolher(item) {
      campo.value = String(item.id);
      dica.textContent = `${item.nome} · ${item.uf}`;
      fechar();
      const form = campo.form;
      if (form) { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }
    }
    function mostrar(termo) {
      lista.innerHTML = "";
      if (!itens.length) {
        const vazio = document.createElement("li");
        vazio.className = "ac-vazio";
        vazio.textContent = "Nenhum ente encontrado";
        lista.appendChild(vazio);
      }
      itens.forEach((item, i) => {
        const li = document.createElement("li");
        li.id = `${idLista}-${i}`;
        li.setAttribute("role", "option");
        li.innerHTML = `<span class="ac-nome">${destacar(item.nome, termo)}${item.capital ? ' <span class="ac-tag">capital</span>' : ""}</span>` +
          `<span class="ac-meta">${escapar(item.uf)} · ${escapar(item.esfera)} · <span class="mono">${item.id}</span></span>`;
        li.addEventListener("mousedown", (e) => { e.preventDefault(); escolher(item); });
        lista.appendChild(li);
      });
      lista.hidden = false;
      campo.setAttribute("aria-expanded", "true");
      ativo = -1;
    }
    async function buscar() {
      const termo = campo.value.trim();
      if (termo.length < 2 && !/^\d+$/.test(termo)) { fechar(); return; }
      if (!termo) { fechar(); return; }
      ultimaBusca = termo;
      try {
        const resposta = await fetch(`/api/entes?q=${encodeURIComponent(termo)}`);
        if (!resposta.ok || termo !== ultimaBusca) return;
        itens = await resposta.json();
        if (document.activeElement === campo) mostrar(termo);
      } catch (e) { fechar(); }
    }

    campo.addEventListener("input", () => {
      dica.textContent = "";
      clearTimeout(espera);
      espera = setTimeout(buscar, 150);
    });
    campo.addEventListener("keydown", (e) => {
      if (lista.hidden) { if (e.key === "ArrowDown" && campo.value.trim()) { buscar(); e.preventDefault(); } return; }
      if (e.key === "ArrowDown") { e.preventDefault(); marcar(Math.min(ativo + 1, itens.length - 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); marcar(Math.max(ativo - 1, 0)); }
      else if (e.key === "Enter" && ativo >= 0 && itens[ativo]) { e.preventDefault(); escolher(itens[ativo]); }
      else if (e.key === "Escape") { e.preventDefault(); fechar(); }
    });
    campo.addEventListener("blur", () => setTimeout(fechar, 120));

    // ao abrir a página com um código já filtrado, mostra o nome do ente ao lado
    const inicial = campo.value.trim();
    if (/^\d{2}$|^\d{7}$/.test(inicial)) {
      fetch(`/api/entes?q=${inicial}&limite=1`).then((r) => r.json()).then((lista) => {
        const item = lista.find((i) => String(i.id) === inicial);
        if (item) dica.textContent = `${item.nome} · ${item.uf}`;
      }).catch(() => {});
    }
  });
})();
