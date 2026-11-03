# Conferência dos valores do coeficiente de participação (IBS)

Ferramenta local para conferir os valores que alimentam a receita média de referência
(LC 227/2026, arts. 114 a 116). O produto principal é a **qualidade dos dados de entrada**:
encontrar a inconsistência, comparar as evidências, registrar a conclusão e saber qual
valor foi adotado, de qual fonte e por quê.

Componentes implementados (art. 115 da LC 227/2026), exercícios 2019 a 2026:

| Componente | Universo | Observação |
|---|---|---|
| ISS municipal (III, a) | 5.570 municípios | — |
| Cota-parte do ICMS creditada (III, b) | 5.570 municípios | Bases líquidas de FUNDEB são identificadas e comparadas com ressalva (regra FLF) |
| ICMS estadual (I, a) | 26 estados | Comparado antes da repartição; a cota repassada é conciliada com a soma das cotas municipais da UF (regra UFC) |
| Contribuições a fundos estaduais (I, b) | 26 estados, 2021–2023 | Aguarda a informação dos Estados ao CGIBS (declaração "Info. UF") |
| ICMS e ISS do Distrito Federal (II, a e b) | DF | Componentes próprios |

Nenhum resultado desta aplicação é a apuração do coeficiente.

## Como usar

```
pip install -r requirements.txt
cd src
python -m validacao servir          # abre http://127.0.0.1:8000
```

Na primeira execução, `servir` faz o seguinte, uma única vez:

1. copia o banco para `data/backup/` (cópia de segurança);
2. migra os dados e as decisões da versão 1 (as tabelas antigas não são alteradas);
3. executa a primeira validação (cerca de 15 segundos).

Outros comandos:

| Comando | O que faz |
|---|---|
| `python -m validacao migrar` | Só a migração, com backup. É idempotente. |
| `python -m validacao validar` | Executa as regras sobre os registros atuais (todos os componentes). |
| `python -m validacao importar-siconfi` | Importa o pacote de RREO e MSC da pasta Bases Conjuntas e valida. Também disponível na aba Importar. |
| `python -m validacao demo --porta 8010` | Cria `data/demonstracao.sqlite` com **dados fictícios** dos 8 cenários de aceitação e abre a tela. |
| `python -m validacao servir-v1` | Telas da versão 1 (legado), na porta 8001. |
| `python -m pytest tests/test_conferencia.py` | Testes das regras e dos cenários de aceitação (instale antes `requirements-dev.txt`). |

## Pacote de recuperação do SICONFI (RREO e MSC)

Arquivos lidos da pasta Bases Conjuntas (ou de uma subpasta imediata):

| Arquivo | Uso |
|---|---|
| `base_validacao_coeficiente.csv` | Obrigatório. `valor_rreo` e `valor_msc` viram registros das declarações RREO e MSC, versionados como qualquer importação. `SEM_RREO`/`SEM_MSC` geram registro sem valor; `*_NAO_SOLICITADO` não gera registro. |
| `rreo_coeficiente.csv` | Conta, coluna e anexo entram na procedência; as linhas aparecem no painel do caso. |
| `msc_coeficiente.csv` | Lançamentos aparecem no painel do caso; a soma de `valor_sinalizado` é conferida contra `valor_msc`. |
| `status.csv` | Consultas com falha aparecem no painel de recuperação e no caso. |
| `cobertura.csv` | Situação do pacote por ente e exercício no painel de recuperação. |

A aba **Importar** mostra as três etapas (arquivos na pasta → importação → validação) e se os arquivos mudaram depois da última
importação, o que entrou por componente (valores, ausências, concordância com a DCA e link para as divergências na Central) e a
conferência automática dos arquivos (RREO = base, soma da MSC = base, valores negativos, consultas com falha).

As linhas de detalhe ficam em `data/detalhes_siconfi.sqlite`, separado do banco principal. RREO e MSC
pertencem ao mesmo declarante (SICONFI): divergem da DCA pela regra DSI, mas não contam como validação independente.

## Jornada principal

Importar → localizar a divergência na central → abrir o caso → comparar fontes e
histórico → registrar a decisão → consultar e exportar o resultado.

- **Visão geral** (tela inicial após entrar): resumo gerencial por componente — entes, casos, diagnóstico,
  andamento da análise, valores nominais por exercício, concordância de cada fonte com a referência, casos
  por tipo de alerta, mapa por UF, maiores divergências pendentes e bases carregadas.
- **Recuperação de dados**: para cada componente, o que foi recuperado de cada fonte por exercício e, por
  ente, uma fila de quadrados (um por exercício) para DCA, RREO, MSC, SIOPE, SIOPS e TCE — valor, zero,
  registro sem valor, consulta com erro, sem registro ou não exigível. Filtros por lacuna na fonte
  principal, falha de consulta e ausência de RREO ou MSC.
- **Central de inconsistências**: uma linha por ente × exercício × componente,
  com a referência e as bases auxiliares lado a lado.
  - Cada linha mostra a maior diferença (em R$ e em %, com o par comparado), os alertas por
    gravidade, a situação da análise e o valor proposto ou adotado.
  - Filtros: ente/código, UF, esfera, exercício, componente, fonte, tipo de alerta, gravidade
    e situação.
  - Ordenação por prioridade, diferença em R$ ou diferença em %.
  - "Analisar" abre um painel lateral sem perder os filtros nem a posição. Alt+↓/↑ navega
    entre os casos.
- **Painel do caso**:
  - valores original, normalizado, proposto e adotado, e a elegibilidade do valor;
  - explicação de cada alerta em linguagem direta, com fórmula e parâmetros;
  - tabela de comparabilidade;
  - matriz fonte × exercício e gráfico da série;
  - registros por versão, com arquivo, aba, linha e coluna de origem;
  - histórico das decisões e formulário de conclusão.
- **Resultado**: valores propostos, adotados e pendentes; diferenças na arrecadação;
  exportação CSV/XLSX da consulta e **pacote de conferência** (ZIP com resultado, alertas,
  decisões, registros, parâmetros, execução e mapeamentos).
- Abas auxiliares:
  - **Importar**: CSV/XLSX com prévia, mapeamento de colunas e relatório de erros.
  - **Dados brutos.**
  - **Cobertura.**
  - **Regras**: catálogo, parâmetros versionados, casos atingidos e exemplos reais.
  - **Fontes e metodologia**: normas, mapeamentos e pendências.

## Princípios implementados

- **SICONFI como referência.** A precedência DCA → RREO é configurável e está marcada como
  hipótese (pendência P1). DCA, RREO e MSC formam um único grupo de independência: a
  concordância entre elas não valida nada.
- **A concordância das bases auxiliares é evidência.** Não há média, votação nem substituição
  automática. Toda troca de fonte é uma decisão humana com justificativa.
- **Comparabilidade antes da diferença.** Cada valor tem conceito (líquido, bruto ou não
  informado) e parcela.
  - Bruto e líquido só são comparados quando existem deduções que permitam a normalização.
  - Sem parcela comum, o par aparece como "Não comparável diretamente", com a razão.
- **Estados distintos**: valor, zero declarado, célula vazia, formato inválido, não
  aplicável, declaração não exigível e valor desconsiderado.
- **Valores em centavos inteiros.** O arredondamento acontece só na exibição.
- **Diferenças:**
  - assinada = fonte − referência;
  - percentual = assinada ÷ |referência|, "não calculável" quando a referência é zero;
  - um limite só é excedido quando o valor é estritamente maior que ele.
- **Registros originais imutáveis.**
  - Reimportar o mesmo conteúdo não muda nada.
  - Um valor diferente cria uma nova versão.
  - Uma decisão guarda a "impressão" das evidências analisadas: se elas mudarem, o caso volta
    como "Reanálise necessária".
- **Diagnóstico separado da análise.** Um caso pode continuar divergente e estar esclarecido.
  Uma mudança de parâmetro cria nova versão das regras e não apaga decisões.

## Regras de qualidade (todas configuráveis e versionadas; parâmetros = hipóteses)

| Código | Regra | O que verifica |
|---|---|---|
| AUS | Ausência na fonte principal | Não há valor na declaração principal (zero conta como valor). |
| INS | Validação insuficiente | Menos fontes independentes comparáveis do que o mínimo configurado. |
| DSI | Divergência entre declarações do SICONFI | DCA × RREO × MSC. |
| DAX | Divergência com base auxiliar | 1% e R$ 1.000 (ambos precisam ser excedidos); gravidade alta acima de 20%. |
| DUP | Duplicidade ou versões conflitantes | O mesmo campo aparece mais de uma vez no mesmo lote. |
| ZER | Zero contra valor positivo | Uma fonte declara zero e outra informa valor positivo. |
| NEG | Valor negativo | Valor negativo em campo que não admite sinal negativo. |
| ESC | Erro de escala ou separador | Razão ≈ 10^k entre fontes ou contra a própria série. |
| REP | Repetição entre exercícios | Mesmo valor, até o centavo, no exercício anterior. |
| REE | Repetição entre entes | Mesmo valor, até o centavo, em outro ente no mesmo exercício. |
| VAR | Variação atípica | Crescimento ou queda acima de 3×, ou pico/vale isolado; gravidade baixa quando corroborada. |
| TOT | Total incompatível com componentes | O total difere da soma das parcelas da mesma declaração. |
| RET | Retificação | Valor vigente com versão > 1 ou extraído após a data de corte. |
| NCP | Não comparável diretamente | A fonte informa outra grandeza (conceito ou abrangência). |
| FLF | Fonte líquida de FUNDEB | A fonte coincide com a referência menos o FUNDEB: diferença de conceito, comparada com ressalva. |
| UFC | Cota repassada × cotas municipais | ICMS estadual: repasse lançado pelo estado contra a soma das cotas declaradas pelos municípios da UF (5%). |
| PAR | Período incompleto | Declaração não exigível ou período parcial. |

## Síntese da versão 1 (o que se manteve e o que mudou)

**Aproveitado:**
- a stack (FastAPI, SQLite, SQLAlchemy, Jinja);
- as cargas das planilhas, com hash e linha de origem;
- o cadastro de entes;
- o de-para das naturezas de receita;
- as decisões com usuário e data;
- a comparação com várias fontes.

**O que dificultava a análise:**
- a fila mostrava códigos de situação sem os valores lado a lado;
- não havia explicação em reais e em percentual;
- a "fonte sugerida" era escolhida por proximidade entre fontes (uma forma de votação);
- não se distinguia conceito bruto de líquido antes de comparar;
- diagnóstico e análise se misturavam;
- havia contadores por bloco em vez de casos;
- o ano de 2026 não era tratado;
- o teste de apuração gravava um relatório fictício na pasta real `relatorios/`.

**Mudanças:**
- a tela inicial passou a ser a tabela comparativa;
- o painel do caso explica cada alerta;
- as regras são independentes e versionadas;
- os registros são versionados;
- as decisões separam a conclusão do valor adotado e sinalizam reanálise;
- foi criada a importação com prévia.

## Pendências de definição (ver aba Fontes e metodologia)

- **P1:** precedência entre DCA e RREO.
- **P2:** conceito de "arrecadação" — bruto ou líquido de restituições.
- **P3:** inclusão do Simples Nacional na DCA.
- **P4:** conceito de SIOPE, SIOPS e TCE (hoje inferido dos dados).
- **P5:** tratamento de 2026.
- **P6:** atualização monetária do § 2º.
- **P7:** critérios de estimativa do art. 116, § 4º.
- **P8:** tolerâncias.
- **P9:** universo (Fernando de Noronha e municípios instalados no período).
- **P10:** fórmula e universo do coeficiente.
- **P11:** ICMS após a cota-parte (valor lançado, 25% ou soma das cotas municipais).
- **P12:** cota-parte antes ou depois do FUNDEB.
- **P13:** fundos estaduais, que dependem da informação dos Estados ao CGIBS.

## Desempenho e operação

Melhorias do diagnóstico de performance (novembro de 2026):

- **Banco fora do OneDrive.** No Windows, os dados ficam em `%LOCALAPPDATA%\ConferenciaCoeficiente`
  (outra pasta: variável `CONFERENCIA_DADOS`). Na primeira execução, o banco de `data/` é copiado para lá; o
  original não é apagado e a pasta antiga recebe um aviso. Uma cópia compactada (~120 MB) vai para
  `data/copia_nuvem/` no máximo uma vez por dia. Cópias de segurança locais: as 2 mais recentes.
- **SQLite ajustado:** cache de 128 MB, leitura por mapeamento de memória, temporários em memória e, fora de
  pastas sincronizadas, `journal_mode=WAL` (as telas leem enquanto a validação grava). Para copiar o banco à
  mão use `python -m validacao backup` — com WAL, copiar só o `.sqlite` pode perder as últimas gravações.
- **Tarefas em processo separado:** validação, importação do pacote e exportações (CSV, XLSX, ZIP) rodam fora do
  servidor; a tela acompanha o andamento e baixa o arquivo quando pronto.
- **Cache dos painéis:** Visão geral, Recuperação, Cobertura, Resultado e as contagens da Central são calculados
  uma vez por versão dos dados (nova validação, importação ou decisão).
- **Índices de cobertura** em `cf_registro` e busca de ente sem acento.

| Comando | O que faz |
|---|---|
| `python -m validacao manutencao` | ANALYZE + VACUUM (também roda sozinho após a validação quando há mais de 20% de páginas livres) |
| `python -m validacao arquivar-v1 --confirmar` | Copia as tabelas da versão 1 para `arquivo_v1_<data>.sqlite` e as esvazia (libera ~130 MB) |
| `python -m validacao copia-nuvem [--forcar]` | Grava agora a cópia compactada na pasta do OneDrive |

## Servidor com Docker

A imagem roda só a aplicação web; o banco e as bases ficam em pastas do servidor montadas como volumes.

```
servidor/
├── conferencia_coeficiente/   ← este repositório (git clone)
│   ├── .env                   ← copiado de env.exemplo e ajustado
│   ├── dados/                 ← validacao.sqlite, detalhes_siconfi.sqlite, backup/, tarefas/
│   └── bases/                 ← pacote do SICONFI (base_validacao_coeficiente.csv, rreo_…, msc_…, cobertura.csv, status.csv)
```

1. **Configuração.** `cp env.exemplo .env` e preencha `CONFERENCIA_SENHA` (senha compartilhada pedida na
   tela de entrada) e `CONFERENCIA_SEGREDO` (`python3 -c "import secrets; print(secrets.token_hex(32))"`).
   A tela de entrada só pede nome e perfil; **sem a senha, qualquer pessoa que alcance o endereço entra.**
2. **Banco.** Pare a aplicação no computador local e copie de `%LOCALAPPDATA%\ConferenciaCoeficiente` os
   arquivos `validacao.sqlite` e `detalhes_siconfi.sqlite` para `dados/` (use `python -m validacao backup` antes,
   para não perder gravações pendentes do WAL). Sem banco, a aplicação cria um vazio.
3. **Bases.** Coloque em `bases/` os arquivos do pacote do SICONFI (a aba Importar lê dessa pasta, montada só
   para leitura).
4. **Permissões.** O processo roda como usuário de uid 1000: `sudo chown -R 1000:1000 dados`.
5. **Subir:** `docker compose up -d --build`. Acompanhe com `docker compose logs -f`; o estado de saúde
   (`docker ps`) vem de `GET /saude`. A primeira subida migra o banco e pode validar (alguns minutos).
6. **HTTPS.** O compose publica a porta só em `127.0.0.1:8000`. Publique por um proxy reverso (nginx, Caddy,
   Traefik) com certificado e, então, defina `CONFERENCIA_COOKIE_SEGURO=1`. Exemplo para nginx:

   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8000;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
       client_max_body_size 200m;   # importação de planilhas
       proxy_read_timeout 300s;
   }
   ```

Comandos de manutenção dentro do contêiner:

```
docker compose exec conferencia python -m validacao backup
docker compose exec conferencia python -m validacao manutencao
docker compose exec conferencia python -m validacao validar
```

Observações: use **um único contêiner e um único processo** (SQLite com um gravador; as tarefas pesadas já
rodam em processo separado). A cópia compactada para o OneDrive fica desligada no servidor
(`CONFERENCIA_COPIA_NUVEM=0`); faça backup da pasta `dados/backup/`. Atualizar: `git pull` e
`docker compose up -d --build`.

## Observações

- **Tamanho do banco.** São guardadas as duas execuções mais recentes (~120 MB cada); o banco fica numa
  pasta local (ver "Desempenho e operação").
- **RREO e MSC** entram pelo pacote do SICONFI (aba Importar ou `importar-siconfi`). **Simples e
  balanços** podem ser importados pela importação de CSV/XLSX.
- **Compensações financeiras** (fora do art. 115) permanecem só nas tabelas da versão 1.
