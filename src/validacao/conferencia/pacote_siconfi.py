"""Importação do pacote de recuperação do SICONFI (RREO e MSC).

Arquivos esperados na mesma pasta (layout definido pela equipe):
  base_validacao_coeficiente.csv  uma linha por ente, exercício e componente (valor_rreo, valor_msc)
  rreo_coeficiente.csv            linhas originais do RREO (Anexo 3, coluna TOTAL ÚLTIMOS 12 MESES)
  msc_coeficiente.csv             lançamentos da MSC usados na reconstrução (soma de valor_sinalizado)
  cobertura.csv                   situação da recuperação por ente e exercício
  status.csv                      situação de cada consulta (fonte, ente, exercício)

Os valores de RREO e MSC viram registros originais (declarações SICONFI_RREO e
SICONFI_MSC), com as mesmas regras de versão da importação manual. As linhas de
detalhe ficam em um banco separado (detalhes_siconfi.sqlite), para não inflar o
banco principal, e aparecem no painel do caso.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from validacao.cadastro import garantir_municipio
from validacao.db import conectar
from validacao.conferencia import modelos as cf
from validacao.conferencia.catalogo import semear_catalogo
from validacao.conferencia.dinheiro import ValorInvalido, para_centavos
from validacao.conferencia.importacao import Linha, persistir
from validacao.modelos import Ente

ARQUIVOS = {
    "base": "base_validacao_coeficiente.csv",
    "rreo": "rreo_coeficiente.csv",
    "msc": "msc_coeficiente.csv",
    "cobertura": "cobertura.csv",
    "status": "status.csv",
}

COMPONENTE = {
    ("M", "ISS"): "ISS_MUN",
    ("D", "ISS"): "ISS_DF",
    ("M", "COTA_PARTE_ICMS"): "COTA_ICMS_MUN",
    ("E", "ICMS"): "ICMS_UF",
    ("D", "ICMS"): "ICMS_DF",
}
CONTA_RREO = {
    "ISS": "ISSLiquidoExcetoTransferenciasEFUNDEB",
    "COTA_PARTE_ICMS": "RREO3CotaParteDoICMS",
    "ICMS": "ICMSLiquidoExcetoTransferenciasEFUNDEB",
}
EVIDENCIA_RREO = (
    "RREO, Anexo 3 (RCL), coluna TOTAL (ÚLTIMOS 12 MESES) do 6º bimestre. Contas 'Líquido exceto transferências e "
    "FUNDEB': receita deduzida de outras deduções, antes da repartição e do FUNDEB — mesmo conceito do total da DCA "
    "(hipótese a confirmar com a taxa de concordância após a carga)."
)
EVIDENCIA_MSC = (
    "MSC de dezembro (MSCE): soma de valor_sinalizado das contas 6212, 62132 e 62139 por natureza de receita, "
    "reconstruída pelo processo de recuperação. Tratada como valor líquido de outras deduções (hipótese)."
)
SITUACOES_PACOTE = ("OK", "DIVERGENCIA", "SEM_RREO", "SEM_MSC", "SEM_RREO_E_SEM_MSC", "RREO_NAO_SOLICITADO", "MSC_NAO_SOLICITADA")


class PacoteInvalido(ValueError):
    pass


def localizar(pasta: Path) -> dict[str, Path]:
    """Procura os arquivos na pasta e, se não estiverem lá, nas subpastas imediatas."""
    pasta = Path(pasta)
    encontrados = {}
    candidatos = [pasta] + sorted([p for p in pasta.iterdir() if p.is_dir() and p.name not in ("validacao",)]) if pasta.is_dir() else []
    for chave, nome in ARQUIVOS.items():
        for base in candidatos:
            caminho = base / nome
            if caminho.is_file():
                encontrados[chave] = caminho
                break
    return encontrados


def descrever(pasta: Path) -> list[dict]:
    achados = localizar(pasta)
    resultado = []
    for chave, nome in ARQUIVOS.items():
        caminho = achados.get(chave)
        if caminho is None:
            resultado.append({"chave": chave, "nome": nome, "presente": False})
        else:
            info = caminho.stat()
            resultado.append({
                "chave": chave, "nome": nome, "presente": True, "caminho": str(caminho), "tamanho": info.st_size,
                "modificado": datetime.fromtimestamp(info.st_mtime).replace(microsecond=0),
            })
    return resultado


def _ler_csv(caminho: Path) -> tuple[pd.DataFrame, bytes]:
    conteudo = caminho.read_bytes()
    texto = None
    for codificacao in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            texto = conteudo.decode(codificacao)
            break
        except UnicodeDecodeError:
            continue
    if texto is None:
        raise PacoteInvalido(f"Não foi possível ler {caminho.name}.")
    primeira = texto.splitlines()[0] if texto else ""
    separador = ";" if primeira.count(";") >= primeira.count(",") else ","
    quadro = pd.read_csv(io.StringIO(texto), sep=separador, dtype=str, keep_default_na=False)
    quadro.columns = [c.strip().lower() for c in quadro.columns]
    return quadro, conteudo


def _inteiro(texto) -> int | None:
    try:
        return int(float(str(texto).strip().replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _valor(texto) -> tuple[int | None, str]:
    texto = str(texto or "").strip()
    if texto == "":
        return None, "AUSENTE"
    try:
        valor = para_centavos(texto)
    except ValorInvalido:
        return None, "INVALIDO"
    if valor is None:
        return None, "AUSENTE"
    return valor, ("ZERO" if valor == 0 else "VALOR")


def importar_pacote(engine, pasta: Path, usuario: str = "sistema", pasta_detalhes: Path | None = None, progresso=None) -> dict:
    arquivos = localizar(pasta)
    if "base" not in arquivos:
        raise PacoteInvalido(f"Arquivo {ARQUIVOS['base']} não encontrado em {pasta}.")
    aviso = progresso or (lambda _m: None)
    aviso("Lendo base de validação...")
    base, conteudo_base = _ler_csv(arquivos["base"])
    faltam = {"cod_ibge", "esfera", "ano", "componente", "valor_rreo", "valor_msc", "situacao"} - set(base.columns)
    if faltam:
        raise PacoteInvalido(f"Colunas ausentes em {ARQUIVOS['base']}: {', '.join(sorted(faltam))}")
    rreo = _ler_csv(arquivos["rreo"])[0] if "rreo" in arquivos else None
    msc = _ler_csv(arquivos["msc"])[0] if "msc" in arquivos else None
    relatorio: dict = {"arquivos": {k: str(v) for k, v in arquivos.items()}, "ocorrencias": []}

    detalhe_rreo = _indice_rreo(rreo)
    soma_msc, contas_msc = _indice_msc(msc)
    agora = datetime.now().replace(microsecond=0)
    data_extracao = datetime.fromtimestamp(arquivos["base"].stat().st_mtime).replace(microsecond=0)

    with Session(engine) as session:
        semear_catalogo(session)
        entes = {e.id_ente: e for e in session.scalars(select(Ente))}
        conhecidos = set(entes)
        linhas_rreo: list[Linha] = []
        linhas_msc: list[Linha] = []
        divergencias_msc = 0
        for posicao, linha in enumerate(base.to_dict("records"), start=2):
            codigo = _inteiro(linha.get("cod_ibge"))
            ano = _inteiro(linha.get("ano"))
            esfera = str(linha.get("esfera", "")).strip().upper()[:1]
            texto_componente = str(linha.get("componente", "")).strip().upper()
            componente = COMPONENTE.get((esfera, texto_componente))
            situacao = str(linha.get("situacao", "")).strip().upper()
            if codigo is None or ano is None or componente is None:
                relatorio["ocorrencias"].append({"linha": posicao, "coluna": "cod_ibge/ano/componente",
                                                 "valor": f"{linha.get('cod_ibge')}/{linha.get('ano')}/{texto_componente}",
                                                 "erro": "Linha sem ente, exercício ou componente reconhecido"})
                continue
            if codigo not in conhecidos:
                if esfera == "M":
                    garantir_municipio(session, codigo, str(linha.get("ente", "")), conhecidos)
                else:
                    relatorio["ocorrencias"].append({"linha": posicao, "coluna": "cod_ibge", "valor": str(codigo),
                                                     "erro": "Ente não encontrado no cadastro"})
                    continue
            periodo = "ANUAL" if ano < agora.year else "PARCIAL"
            comum = {"arquivo": ARQUIVOS["base"], "linha": posicao, "situacao_pacote": situacao,
                     "tolerancia_pacote": linha.get("tolerancia_aplicada", "")}
            if situacao != "RREO_NAO_SOLICITADO":
                valor, estado = _valor(linha.get("valor_rreo"))
                procedencia = {**comum, "coluna": "valor_rreo"}
                det = detalhe_rreo.get((codigo, ano, texto_componente))
                if det:
                    procedencia.update(det)
                linhas_rreo.append(Linha(codigo, ano, CONTA_RREO[texto_componente], "TOTAL", "LIQUIDO",
                                         str(linha.get("valor_rreo", "")), valor, estado, procedencia,
                                         componente=componente, periodo=periodo))
            if situacao != "MSC_NAO_SOLICITADA":
                valor, estado = _valor(linha.get("valor_msc"))
                procedencia = {**comum, "coluna": "valor_msc"}
                chave = (codigo, ano, texto_componente)
                if chave in soma_msc:
                    procedencia["lancamentos"] = soma_msc[chave]["n"]
                    procedencia["contas"] = ", ".join(sorted(contas_msc[chave]))[:200]
                    if valor is not None and abs(soma_msc[chave]["soma"] - valor) > 100:
                        divergencias_msc += 1
                        procedencia["soma_lancamentos_difere"] = soma_msc[chave]["soma"]
                linhas_msc.append(Linha(codigo, ano, f"MSC_{texto_componente}", "TOTAL", "LIQUIDO",
                                        str(linha.get("valor_msc", "")), valor, estado, procedencia,
                                        componente=componente, periodo=periodo))
        resultado = {}
        for declaracao, linhas, evidencia in (("SICONFI_RREO", linhas_rreo, EVIDENCIA_RREO), ("SICONFI_MSC", linhas_msc, EVIDENCIA_MSC)):
            aviso(f"Gravando {declaracao}...")
            resultado[declaracao] = _gravar_lote(session, declaracao, linhas, conteudo_base, arquivos["base"], usuario,
                                                 data_extracao, evidencia, relatorio)
        if divergencias_msc:
            relatorio["ocorrencias"].append({"linha": "", "coluna": "valor_msc", "valor": str(divergencias_msc),
                                             "erro": "Casos em que a soma dos lançamentos da MSC difere de valor_msc em mais de R$ 1,00"})
        if "status" in arquivos:
            aviso("Gravando situação das consultas...")
            resultado["status"] = _gravar_status(session, _ler_csv(arquivos["status"])[0], agora)
        if "cobertura" in arquivos:
            resultado["cobertura"] = _gravar_cobertura(session, _ler_csv(arquivos["cobertura"])[0], agora)
        session.commit()
    aviso("Conferindo os arquivos...")
    try:
        quadros = {"base": base, "rreo": rreo, "msc": msc}
        for chave in ("status", "cobertura"):
            if chave in arquivos:
                quadros[chave] = _ler_csv(arquivos[chave])[0]
        diagnostico = diagnosticar(arquivos, quadros)
    except Exception as erro:  # o diagnóstico é informativo
        diagnostico = {"erro": str(erro)}
    resultado["diagnostico"] = diagnostico
    from validacao.conferencia.migracao import definir_configuracao as _definir

    with Session(engine) as session:
        _definir(session, "pacote_siconfi_diagnostico", diagnostico)
        _definir(session, "pacote_siconfi_resultado", {
            k: v for k, v in resultado.items() if k.startswith("SICONFI_") or k in ("status", "cobertura")
        })
        session.commit()
    if pasta_detalhes is not None and (rreo is not None or msc is not None):
        aviso("Gravando detalhes do RREO e da MSC...")
        resultado["detalhes"] = gravar_detalhes(pasta_detalhes, rreo, msc)
        from validacao.conferencia.migracao import definir_configuracao

        with Session(engine) as session:
            definir_configuracao(session, "detalhes_siconfi", str(caminho_detalhes(pasta_detalhes)))
            definir_configuracao(session, "pacote_siconfi", {
                "importado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pasta": str(pasta),
                "arquivos": {k: str(v) for k, v in arquivos.items()},
            })
            session.commit()
    resultado["ocorrencias"] = relatorio["ocorrencias"]
    return resultado


def _num(serie) -> pd.Series:
    return pd.to_numeric(serie.astype(str).str.replace(",", ".", regex=False), errors="coerce")


def _contagem(serie) -> dict:
    return {str(k): int(v) for k, v in serie.value_counts().items()}


def diagnosticar(arquivos: dict[str, Path], quadros: dict[str, pd.DataFrame | None]) -> dict:
    """Confere os arquivos do pacote entre si e descreve o que contêm (informativo, não altera dados)."""
    d: dict = {"gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M"), "arquivos": {}, "observacoes": []}
    obs = d["observacoes"]
    for chave, caminho in arquivos.items():
        quadro = quadros.get(chave)
        info = caminho.stat()
        d["arquivos"][chave] = {
            "linhas": None if quadro is None else int(len(quadro)), "tamanho": info.st_size,
            "modificado": datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
    base, rreo, msc = quadros.get("base"), quadros.get("rreo"), quadros.get("msc")
    chave_cols = ["cod_ibge", "ano", "componente"]
    if base is not None:
        b = base.copy()
        b["componente"] = b["componente"].str.strip().str.upper()
        d["exercicios"] = sorted({int(a) for a in _num(b["ano"]).dropna()})
        d["situacoes"] = {
            f"{e}|{c}": _contagem(grupo["situacao"].str.upper())
            for (e, c), grupo in b.groupby([b["esfera"].str.upper(), b["componente"]])
        }
        duplicadas = int(b.duplicated(chave_cols).sum())
        if duplicadas:
            obs.append(("alerta", f"A base de validação tem {duplicadas} linha(s) repetida(s) para o mesmo ente, exercício e componente."))
    if rreo is not None and base is not None:
        r = rreo.copy()
        r["componente"] = r["componente"].str.strip().str.upper()
        d["rreo"] = {
            "contas": _contagem(r["cod_conta"]),
            "colunas": _contagem(r["coluna"]) if "coluna" in r else {},
            "anexos": _contagem(r["anexo"]) if "anexo" in r else {},
            "negativos": int((_num(r["valor"]) < 0).sum()),
            "repetidas": int(r.duplicated(chave_cols).sum()),
        }
        juntos = r.merge(b[chave_cols + ["valor_rreo"]], on=chave_cols, how="left")
        diferentes = int(((_num(juntos["valor"]) - _num(juntos["valor_rreo"])).abs() > 0.005).sum())
        d["rreo"]["difere_da_base"] = diferentes
        obs.append(("ok" if not diferentes else "alerta",
                    "Os valores do RREO conferem, linha a linha, com valor_rreo da base de validação." if not diferentes else
                    f"{diferentes} linha(s) do RREO têm valor diferente de valor_rreo na base de validação."))
        if d["rreo"]["negativos"]:
            obs.append(("info", f"O RREO tem {d['rreo']['negativos']} valor(es) negativo(s); aparecem como alerta NEG nos casos."))
    if msc is not None and base is not None:
        m = msc.copy()
        m["componente"] = m["componente"].str.strip().str.upper()
        m["_v"] = _num(m["valor_sinalizado"])
        somas = m.groupby(chave_cols)["_v"].sum().reset_index()
        tipos = m.groupby(chave_cols)["tipo_valor"].agg(lambda x: "+".join(sorted(set(x)))).reset_index()
        juntos = b[chave_cols + ["valor_msc"]].merge(somas, on=chave_cols, how="inner")
        diferentes = int(((juntos["_v"] - _num(juntos["valor_msc"])).abs() > 0.05).sum())
        por_tipo = b[chave_cols + ["valor_msc"]].merge(tipos, on=chave_cols, how="left")
        negativos = por_tipo[_num(por_tipo["valor_msc"]) < 0]
        d["msc"] = {
            "contas": _contagem(m["conta_contabil"]),
            "tipos_valor_por_caso": _contagem(tipos["tipo_valor"]),
            "matrizes": _contagem(m["tipo_matriz"]) if "tipo_matriz" in m else {},
            "meses": _contagem(m["mes_referencia"]) if "mes_referencia" in m else {},
            "difere_da_base": diferentes,
            "negativos": int(len(negativos)),
            "negativos_por_tipo": _contagem(negativos["tipo_valor"].fillna("?")),
            "negativos_por_ano": {str(k): int(v) for k, v in negativos["ano"].value_counts().sort_index().items()},
        }
        obs.append(("ok" if not diferentes else "alerta",
                    "A soma de valor_sinalizado dos lançamentos da MSC confere com valor_msc da base de validação." if not diferentes else
                    f"Em {diferentes} caso(s) a soma dos lançamentos da MSC difere de valor_msc."))
        if len(negativos):
            obs.append(("alerta",
                        f"{len(negativos)} caso(s) têm valor da MSC negativo ("
                        + ", ".join(f"{n} com {t}" for t, n in d["msc"]["negativos_por_tipo"].items())
                        + "). Receita realizada não deveria ser negativa: é provável que a matriz de encerramento (MSCE) "
                          "traga lançamentos de encerramento no period_change desses entes. São importados como estão "
                          "e aparecem como divergência (DSI) e valor negativo (NEG); vale reextraí-los pela MSC de dezembro "
                          "(MSCC, ending_balance) — hipótese a confirmar."))
    status = quadros.get("status")
    if status is not None:
        ok = status["ok"].astype(str).str.strip()
        d["consultas"] = {f"{f}|{o}": int(n) for (f, o), n in status.groupby([status["fonte"].str.lower(), ok]).size().items()}
        falhas = status[~ok.isin(["1", "true", "True"])]
        d["falhas"] = [
            {"fonte": x.get("fonte"), "cod_ibge": x.get("cod_ibge"), "ente": x.get("ente"), "ano": x.get("ano"),
             "erro": str(x.get("erro", ""))[:160]}
            for x in falhas.head(50).to_dict("records")
        ]
        if len(falhas):
            obs.append(("alerta", f"{len(falhas)} consulta(s) ao SICONFI falharam e precisam ser refeitas (lista abaixo)."))
        else:
            obs.append(("ok", "Todas as consultas ao SICONFI registradas em status.csv tiveram sucesso."))
    cobertura = quadros.get("cobertura")
    if cobertura is not None:
        d["cobertura"] = _contagem(cobertura["situacao"].str.upper())
    return d


def _indice_rreo(rreo: pd.DataFrame | None) -> dict:
    indice = {}
    if rreo is None:
        return indice
    for posicao, linha in enumerate(rreo.to_dict("records"), start=2):
        chave = (_inteiro(linha.get("cod_ibge")), _inteiro(linha.get("ano")), str(linha.get("componente", "")).strip().upper())
        if chave in indice:
            indice[chave]["linhas_rreo"] = indice[chave].get("linhas_rreo", 1) + 1
            continue
        indice[chave] = {
            "cod_conta": linha.get("cod_conta", ""), "conta": str(linha.get("conta", ""))[:120],
            "coluna_rreo": linha.get("coluna", ""), "anexo": linha.get("anexo", ""),
            "demonstrativo": linha.get("demonstrativo", ""), "linha_rreo": posicao,
        }
    return indice


def _indice_msc(msc: pd.DataFrame | None):
    somas: dict = defaultdict(lambda: {"soma": 0, "n": 0})
    contas: dict = defaultdict(set)
    if msc is None:
        return somas, contas
    for linha in msc.to_dict("records"):
        chave = (_inteiro(linha.get("cod_ibge")), _inteiro(linha.get("ano")), str(linha.get("componente", "")).strip().upper())
        valor, _estado = _valor(linha.get("valor_sinalizado"))
        somas[chave]["soma"] += valor or 0
        somas[chave]["n"] += 1
        if linha.get("conta_contabil"):
            contas[chave].add(str(linha.get("conta_contabil")))
    return somas, contas


def _gravar_lote(session, declaracao, linhas, conteudo, arquivo, usuario, data_extracao, evidencia, relatorio) -> dict:
    hash_conteudo = hashlib.sha256(conteudo + declaracao.encode()).hexdigest()
    existente = session.scalar(select(cf.Lote).where(cf.Lote.hash_conteudo == hash_conteudo, cf.Lote.situacao == "CONCLUIDO"))
    if existente is not None:
        return {"lote": existente.id, "situacao": "JA_IMPORTADO", "novos": 0, "repetidos": 0, "novas_versoes": 0}
    mapeamentos = {}
    for campo in sorted({l.campo for l in linhas}):
        atual = session.scalar(select(cf.Mapeamento).where(cf.Mapeamento.declaracao_id == declaracao, cf.Mapeamento.campo == campo))
        if atual is None:
            exemplo = next(l for l in linhas if l.campo == campo)
            atual = cf.Mapeamento(
                declaracao_id=declaracao, campo=campo, componente_id=exemplo.componente or "ISS_MUN", exercicio_inicio=2019,
                exercicio_fim=2026, parcela="TOTAL", conceito="LIQUIDO",
                origem_detalhe=f"{ARQUIVOS['base']}, coluna {'valor_rreo' if declaracao == 'SICONFI_RREO' else 'valor_msc'}"
                               + (f" (conta {campo})" if declaracao == "SICONFI_RREO" else ""),
                abrangencia="{}", status="HIPOTESE", evidencia=evidencia, versao=1,
            )
            session.add(atual)
            session.flush()
        mapeamentos[campo] = atual.id
    lote = cf.Lote(
        declaracao_id=declaracao, origem_tipo="ARQUIVO", arquivo=str(arquivo), aba=None,
        parametros=json.dumps({"pacote": "SICONFI", "arquivo": ARQUIVOS["base"]}, ensure_ascii=False),
        hash_conteudo=hash_conteudo, data_extracao=data_extracao, data_importacao=datetime.now().replace(microsecond=0),
        usuario=usuario, situacao="EM_ANDAMENTO", observacao="Pacote de recuperação do SICONFI (RREO e MSC).",
    )
    session.add(lote)
    session.flush()
    contagem = persistir(session, lote, linhas, linhas[0].componente if linhas else "ISS_MUN", "ANUAL", data_extracao, mapeamentos)
    ocorrencias = [o for o in relatorio["ocorrencias"]]
    lote.erros = len(ocorrencias)
    lote.relatorio = json.dumps({"erros": ocorrencias[:5000]}, ensure_ascii=False)
    lote.situacao = "CONCLUIDO"
    session.flush()
    return {"lote": lote.id, "situacao": "IMPORTADO", **contagem}


def _bool(texto) -> bool | None:
    valor = str(texto or "").strip().lower()
    if valor in ("true", "1", "sim", "s", "ok", "verdadeiro", "t"):
        return True
    if valor in ("false", "0", "nao", "não", "n", "falso", "f"):
        return False
    return None


def _gravar_status(session: Session, quadro: pd.DataFrame, agora: datetime) -> int:
    session.execute(delete(cf.Consulta))
    linhas = []
    for linha in quadro.to_dict("records"):
        codigo, ano = _inteiro(linha.get("cod_ibge")), _inteiro(linha.get("ano"))
        if codigo is None or ano is None:
            continue
        linhas.append({
            "fonte": str(linha.get("fonte", "")).strip().upper()[:30], "id_ente": codigo, "ano": ano, "ok": _bool(linha.get("ok")),
            "linhas_retidas": _inteiro(linha.get("linhas_retidas")), "linhas_lidas": _inteiro(linha.get("linhas_lidas")),
            "paginas": _inteiro(linha.get("paginas")), "detalhe": str(linha.get("detalhe", ""))[:500],
            "erro": str(linha.get("erro", ""))[:1000], "atualizado_em": str(linha.get("atualizado_em", ""))[:40], "importado_em": agora,
        })
    for inicio in range(0, len(linhas), 2000):
        session.execute(insert(cf.Consulta), linhas[inicio:inicio + 2000])
    return len(linhas)


def _gravar_cobertura(session: Session, quadro: pd.DataFrame, agora: datetime) -> int:
    session.execute(delete(cf.CoberturaPacote))
    linhas = []
    vistos = set()
    for linha in quadro.to_dict("records"):
        codigo, ano = _inteiro(linha.get("cod_ibge")), _inteiro(linha.get("ano"))
        if codigo is None or ano is None or (codigo, ano) in vistos:
            continue
        vistos.add((codigo, ano))
        linhas.append({
            "id_ente": codigo, "ano": ano,
            "componentes_esperados": str(linha.get("componentes_esperados", ""))[:120],
            "componentes_rreo": str(linha.get("componentes_rreo", ""))[:120],
            "componentes_msc": str(linha.get("componentes_msc", ""))[:120],
            "consultas_rreo_ok": str(linha.get("consultas_rreo_ok", ""))[:20],
            "consultas_msc_ok": str(linha.get("consultas_msc_ok", ""))[:20],
            "validacoes_ok": _inteiro(linha.get("validacoes_ok")), "divergencias": _inteiro(linha.get("divergencias")),
            "ausencias": _inteiro(linha.get("ausencias")), "situacao": str(linha.get("situacao", "")).strip().upper()[:30],
            "importado_em": agora,
        })
    for inicio in range(0, len(linhas), 2000):
        session.execute(insert(cf.CoberturaPacote), linhas[inicio:inicio + 2000])
    return len(linhas)


# ---------------------------------------------------------------- detalhes

def caminho_detalhes(pasta_dados: Path) -> Path:
    return Path(pasta_dados) / "detalhes_siconfi.sqlite"


def gravar_detalhes(pasta_dados: Path, rreo: pd.DataFrame | None, msc: pd.DataFrame | None) -> dict:
    destino = caminho_detalhes(pasta_dados)
    destino.parent.mkdir(parents=True, exist_ok=True)
    resumo = {}
    with conectar(destino) as conexao:
        for nome, quadro in (("rreo", rreo), ("msc", msc)):
            if quadro is None:
                continue
            copia = quadro.copy()
            copia.insert(0, "linha_arquivo", range(2, len(copia) + 2))
            copia["cod_ibge"] = pd.to_numeric(copia["cod_ibge"], errors="coerce").astype("Int64")
            copia["ano"] = pd.to_numeric(copia["ano"], errors="coerce").astype("Int64")
            copia["componente"] = copia["componente"].str.strip().str.upper()
            copia.to_sql(nome, conexao, if_exists="replace", index=False, chunksize=5000)
            conexao.execute(f"CREATE INDEX IF NOT EXISTS ix_{nome}_caso ON {nome} (cod_ibge, ano, componente)")
            resumo[nome] = len(copia)
        conexao.execute("CREATE TABLE IF NOT EXISTS carga (quando TEXT, rreo INTEGER, msc INTEGER)")
        conexao.execute("INSERT INTO carga VALUES (?, ?, ?)", (datetime.now().isoformat(timespec="seconds"), resumo.get("rreo"), resumo.get("msc")))
    return resumo


COMPONENTE_PACOTE = {"ISS_MUN": "ISS", "ISS_DF": "ISS", "COTA_ICMS_MUN": "COTA_PARTE_ICMS", "ICMS_UF": "ICMS", "ICMS_DF": "ICMS"}


def detalhes_do_caso(destino: Path | str | None, id_ente: int, ano: int, componente: str) -> dict:
    if not destino:
        return {}
    destino = Path(destino)
    if not destino.is_file() or componente not in COMPONENTE_PACOTE:
        return {}
    chave = (id_ente, ano, COMPONENTE_PACOTE[componente])
    resultado = {}
    with conectar(destino) as conexao:
        conexao.row_factory = sqlite3.Row
        tabelas = {linha[0] for linha in conexao.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for nome in ("rreo", "msc"):
            if nome not in tabelas:
                continue
            linhas = conexao.execute(
                f"SELECT * FROM {nome} WHERE cod_ibge = ? AND ano = ? AND componente = ? LIMIT 500", chave
            ).fetchall()
            resultado[nome] = [dict(l) for l in linhas]
    return resultado
