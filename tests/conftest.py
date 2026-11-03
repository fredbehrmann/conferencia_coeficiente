"""Os testes da versão 1 (aceite, apuração, verificação...) usam as planilhas reais da pasta
Bases Conjuntas, que não vão para o repositório. Sem elas, esses testes são pulados."""

import pytest

from pathlib import Path

PLANILHA_DE_REFERENCIA = Path(__file__).resolve().parents[2] / "SICONFI_DCA_Receitas_2019_2025.xlsx"


def pytest_collection_modifyitems(config, items):
    if PLANILHA_DE_REFERENCIA.exists():
        return
    pular = pytest.mark.skip(reason=f"planilhas reais ausentes ({PLANILHA_DE_REFERENCIA.name}); rode na pasta Bases Conjuntas")
    for item in items:
        if item.fspath.basename != "test_conferencia.py":
            item.add_marker(pular)
