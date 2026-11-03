"""Cache em memória dos painéis, invalidado pela versão dos dados.

Os painéis (Visão geral, Recuperação, Cobertura, Resultado, contagens da Central) só mudam
quando há nova validação, nova importação ou nova decisão. A chave de cada valor inclui a
"versão dos dados" — os maiores ids de execução concluída, decisão e lote, e a quantidade de
decisões —, que muda a cada uma dessas gravações. Não há expiração por tempo nem limpeza manual.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from sqlalchemy import text
from sqlalchemy.orm import Session

_SQL_VERSAO = text(
    "SELECT (SELECT max(id) FROM cf_execucao WHERE situacao = 'CONCLUIDA'), (SELECT max(id) FROM cf_decisao), "
    "(SELECT count(*) FROM cf_decisao), (SELECT max(id) FROM cf_lote), "
    "(SELECT valor FROM cf_configuracao WHERE chave = 'pacote_siconfi')"
)


def versao_dados(session: Session) -> tuple:
    return tuple(session.execute(_SQL_VERSAO).one())


class CachePorVersao:
    def __init__(self, maximo: int = 128):
        self._itens: OrderedDict = OrderedDict()
        self._trava = threading.Lock()
        self.maximo = maximo
        self.acertos = 0
        self.faltas = 0

    def obter(self, session: Session, chave: tuple, calcular):
        completa = (chave, versao_dados(session))
        with self._trava:
            if completa in self._itens:
                self._itens.move_to_end(completa)
                self.acertos += 1
                return self._itens[completa]
        valor = calcular()
        with self._trava:
            self.faltas += 1
            self._itens[completa] = valor
            self._itens.move_to_end(completa)
            while len(self._itens) > self.maximo:
                self._itens.popitem(last=False)
        return valor

    def limpar(self) -> None:
        with self._trava:
            self._itens.clear()
