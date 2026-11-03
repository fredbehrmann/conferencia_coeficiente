# Conferência dos valores do coeficiente de participação (IBS) — imagem do servidor web
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # banco, cópias de segurança, arquivos importados e tarefas (volume persistente)
    CONFERENCIA_DADOS=/dados \
    # planilhas e pacote do SICONFI (RREO/MSC) lidos pela aba Importar
    CONFERENCIA_BASES=/bases \
    CONFERENCIA_RELATORIOS=/dados/relatorios \
    CONFERENCIA_HOST=0.0.0.0 \
    CONFERENCIA_PORTA=8000 \
    # a cópia compactada para o OneDrive só faz sentido no computador local
    CONFERENCIA_COPIA_NUVEM=0 \
    TZ=America/Sao_Paulo

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ ./src/
COPY fontes/ ./fontes/

RUN useradd --create-home --uid 1000 conferencia \
 && mkdir -p /dados /bases \
 && chown -R conferencia:conferencia /dados /bases /app

USER conferencia
WORKDIR /app/src
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"CONFERENCIA_PORTA\", \"8000\")}/saude', timeout=4)" || exit 1

CMD ["python", "-m", "validacao", "servir"]
