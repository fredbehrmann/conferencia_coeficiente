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
| `python -m validacao importar-bases [planilhas...]` | Importa DCA, SIOPS, SIOPE e TCE (layout fixo) e valida se algo mudou. Sem argumentos, usa as planilhas da pasta Bases Conjuntas. Também na aba Importar. |
| `python -m validacao demo --porta 8010` | Cria `data/demonstracao.sqlite` com **dados fictícios** dos 8 cenários de aceitação e abre a tela. |
| `python -m validacao servir-v1` | Telas da versão 1 (legado), na porta 8001. |
| `python -m pytest tests/test_conferencia.py` | Testes das regras e dos cenários de aceitação (instale antes `requirements-dev.txt`). |

## Pacote de recuperação do SICONFI (RREO e MSC)

Os arquivos podem vir de dois lugares: da pasta Bases Conjuntas (ou de uma subpasta imediata) ou **enviados pela
aba Importar** — os cinco CSV soltos ou um ZIP com eles (o ZIP do pacote completo tem ~10 MB, contra ~77 MB dos CSV).
No envio, cada arquivo é reconhecido pelo cabeçalho, não pelo nome; a base de validação é obrigatória e um arquivo
fora do layout recusa o envio inteiro. O pacote enviado fica em `<pasta do banco>/pacote_siconfi/` e substitui o
envio anterior por inteiro. Vale o mais recente entre a pasta e o envio. A data de extração do lote vem do
`atualizado_em` mais recente do `status.csv`.

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

## Bases de layout fixo (DCA, SIOPS, SIOPE e Tribunais de Contas)

Na aba **Importar**, envie uma ou mais destas planilhas (de uma vez); o tipo é reconhecido pelas abas, não pelo nome:

| Base | Planilha usual | Abas lidas | Campos |
|---|---|---|---|
| DCA — SICONFI | `SICONFI_DCA_Receitas_2019_2025.xlsx` | `SICONFI` e `SICONFI_BRUTO` | ISS, cota-parte e ICMS (totais), cota repassada; bruto, FUNDEB, deduções e FECOP |
| SIOPS | `SIOPS - ICMS e ISS.xlsx` | `Municipios` e `Estados` | ISS, cota-parte, ICMS e cota repassada |
| SIOPE | `Dados SIOPE - ICMS e ISS.xlsx` | `Municipios` e `Estados` | ISS e ICMS com abertura (principal, multas, dívida ativa, FECOP) e cota-parte |
| Tribunais de Contas | `TCE Arrecadacao Municipios - 2019_2025.xlsx` | `Base detalhada` (+ `ISS` e `Leia-me`) | Abertura do ISS e da cota-parte, com a natureza do valor informada pelo tribunal |

A leitura repete a carga original da versão 1 (mesmas colunas, campos, parcelas e conceitos). Reimportar as
planilhas atuais sobre o banco migrado dá **0 novos e 0 versões** nos 637.282 valores (conferido). Numa planilha
atualizada, só o que mudou vira nova versão; casos já decididos voltam para reanálise. Arquivo idêntico a um já
importado é ignorado. A DCA é processada primeiro porque completa o cadastro de entes; códigos sem ente vão para
o relatório de ocorrências do lote, sem interromper a importação. Células vazias não geram registro e compensações
financeiras (fora do art. 115) são ignoradas. A data de extração vem do `Leia-me` (TCE) ou das propriedades da planilha.

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

## Controle de acesso

Só entra quem tem conta: login e senha individuais. A senha é guardada apenas como hash (scrypt com sal);
nem o administrador consegue lê-la depois. O nome da conta é o que assina as decisões.

- **Primeira subida:** a aplicação cria a conta `admin` (perfil Administrador) com a senha inicial entregue ao
  responsável, ou com `CONFERENCIA_ADMIN_SENHA`, se definida. Troque-a em **Minha senha** (canto superior direito).
- **Novas contas:** aba **Usuários** (só administradores). Informe nome, login, perfil e a senha inicial
  (já vem uma sugestão forte). A senha aparece uma única vez na tela; por padrão a pessoa é obrigada a trocá-la
  no primeiro acesso. Pessoas que já assinavam decisões antes do controle de acesso aparecem como "sem login":
  crie a conta com o mesmo nome para ligar o histórico.
- **Esqueceu a senha:** o administrador clica em **Redefinir senha** e entrega a provisória. Desativar uma conta
  encerra na hora as sessões abertas dela.
- **Regras:** senha com 12 caracteres ou mais e três tipos entre minúsculas, maiúsculas, números e símbolos;
  5 tentativas erradas bloqueiam a conta por 15 minutos; a sessão expira em 12 horas. Sempre fica ao menos um
  administrador ativo.

Pela linha de comando (também serve para recuperar o `admin`):

| Comando | O que faz |
|---|---|
| `python -m validacao usuario listar` | Lista as contas |
| `python -m validacao usuario criar maria.souza --nome "Maria Souza" --perfil ANALISTA` | Cria a conta e mostra a senha provisória |
| `python -m validacao usuario senha admin` | Gera uma senha provisória nova (desbloqueia e reativa a conta) |
| `python -m validacao usuario desativar maria.souza` | Desativa (`ativar` reativa) |

No Docker, prefixe com `docker compose exec conferencia`.

## Servidor com Docker

A imagem roda só a aplicação web; o banco e as bases ficam em pastas do servidor montadas como volumes.

```
servidor/
├── conferencia_coeficiente/   ← este repositório (git clone)
│   ├── .env                   ← copiado de env.exemplo e ajustado
│   ├── dados/                 ← validacao.sqlite, detalhes_siconfi.sqlite, backup/, tarefas/
│   └── bases/                 ← pacote do SICONFI (base_validacao_coeficiente.csv, rreo_…, msc_…, cobertura.csv, status.csv)
```

1. **Configuração.** `cp env.exemplo .env` e preencha `CONFERENCIA_SEGREDO`
   (`python3 -c "import secrets; print(secrets.token_hex(32))"`). As contas de acesso ficam no próprio banco
   (ver "Controle de acesso"); ao levar o banco do computador local, as contas vão junto.
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

### Publicar na Railway

O `railway.toml` já indica o Dockerfile e a verificação de saúde (`/saude`). No projeto da Railway:

1. **Volume:** no serviço, *Add Volume* com ponto de montagem **`/dados`** (banco, contas, cópias e tarefas).
   Sem volume, tudo se perde a cada deploy. O contêiner acerta sozinho o dono do volume (montado como root).
2. **Variáveis** (*Variables*):
   - `CONFERENCIA_SEGREDO` = resultado de `python -c "import secrets; print(secrets.token_hex(32))"`
   - `CONFERENCIA_COOKIE_SEGURO=1` (a Railway serve por HTTPS)
   - `CONFERENCIA_PROXIES=*` (o proxy da Railway não tem IP fixo)
   - `CONFERENCIA_BASES=/dados/bases` (a Railway permite um volume por serviço)
   - opcional: `CONFERENCIA_ADMIN_SENHA`, para a conta `admin` de um banco novo
   A porta vem da variável `PORT`, que a própria Railway define.
3. **Endereço:** *Settings → Networking → Generate Domain*.

O volume começa vazio: a aplicação sobe com um banco novo e a conta `admin`. Para usar o banco atual, copie
`validacao.sqlite` e `detalhes_siconfi.sqlite` para `/dados` com a aplicação parada.

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
