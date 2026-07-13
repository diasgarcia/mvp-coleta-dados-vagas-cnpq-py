# Coleta de Vagas de Tecnologia — Implementação Python

## Ideia do projeto

Implementação Python pequena e auditável para consultar a TheirStack e a SerpApi,
registrar cada execução, preservar cada resposta HTTP sanitizada e salvar as vagas
reconhecidas individualmente no PostgreSQL.

Esta é uma reimplementação mais simples, com finalidade acadêmica e foco inicial em
vagas de tecnologia no estado de São Paulo. O projeto coleta, preserva e exporta dados
rastreáveis; ainda não faz análise estatística, extração de competências,
deduplicação global, dashboard ou publicação de dataset.

## Tecnologias

- Python 3.12+;
- `httpx.Client` síncrono para as APIs;
- `psycopg` e SQL explícito, sem ORM;
- `python-dotenv`;
- PostgreSQL 16 no Docker Desktop e Docker Compose;
- pytest e Ruff.

## Projetos e bancos separados

- `mvp-coleta-dados-vagas-cnpq-ts`: implementação original em TypeScript, mantida como projeto independente;
- `mvp-coleta-dados-vagas-cnpq-py`: esta implementação Python;
- TypeScript: banco `job_market`, porta externa `5432`;
- Python: banco `job_market-py`, porta externa `5433`, container
  `job_market_postgres_py` e volume `postgres_data_py`.

Os comandos deste README devem ser executados dentro da pasta `-py`. O Compose Python
não reutiliza nem remove o container, o volume ou o banco TypeScript. Não execute
`docker compose down -v` se quiser preservar os dados locais.

## Arquitetura raw-first

O fluxo por resposta HTTP é direto:

1. cria uma linha `running` em `collection_runs`;
2. reutiliza uma conexão PostgreSQL e um `httpx.Client` durante a coleta;
3. consulta o fornecedor;
4. sanitiza segredos eventualmente ecoados;
5. grava `raw_api_responses` e executa `commit` imediatamente;
6. somente depois classifica e mapeia as vagas;
7. grava `raw_jobs` e o progresso em outra transação;
8. conclui a execução como `success`, `partial` ou `failed`.

Os campos desconhecidos e tokens opacos de paginação são preservados. Chaves de API,
headers de autorização, cookies e credenciais em URLs são redigidos.

## Datas de publicação

`raw_jobs.published_date` é o campo indicado para análises no nível de dia. A
`publication_date_source` registra a qualidade dessa data:

- `theirstack_exact`: dia fornecido diretamente em `date_posted`, sem conversão de fuso;
- `serpapi_estimated`: estimativa calculada a partir do texto relativo e do
  `collected_at` da própria vaga;
- `missing`: a fonte não informou quando a vaga foi publicada;
- `unrecognized`: o texto original existe, mas seu formato não foi reconhecido.

Na SerpApi, `published_at_text` continua preservando valores como `há 18 dias` e a
estimativa nunca deve ser apresentada como uma data exata do anunciante. `collected_at`
registra quando a coleta ocorreu. `published_at` permanece por compatibilidade, mas não
é preenchido artificialmente para a SerpApi.

## Estrutura resumida

```text
.
├── job_collector/
│   ├── collector.py
│   ├── config.py
│   ├── db.py
│   ├── main.py
│   ├── monthly.py
│   ├── regions.py
│   ├── sanitize.py
│   ├── theirstack.py
│   └── serpapi.py
├── migrations/
├── scripts/
├── tests/
└── results/
    ├── theirstack.json
    ├── serpapi.json
    └── monthly/<AAAA-MM>/
```

## Requisitos

- Windows com PowerShell;
- Python 3.12 ou superior;
- Docker Desktop usando containers Linux;
- credenciais válidas da TheirStack e da SerpApi apenas para coletas reais.

Não é necessário navegador, WSL Ubuntu ou PostgreSQL instalado no host para executar
este projeto.

## Preparação no Windows PowerShell

Crie o ambiente virtual e instale a aplicação com as ferramentas de desenvolvimento:

```powershell
git clone https://github.com/diasgarcia/mvp-coleta-dados-vagas-cnpq-py.git
cd mvp-coleta-dados-vagas-cnpq-py
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Crie a configuração local:

```powershell
Copy-Item .env.example .env
```

Preencha `THEIRSTACK_API_KEY` e `SERPAPI_API_KEY` no `.env` local. Não compartilhe o
arquivo, não cole seus valores em comandos e não os inclua em logs. O `.env` já está no
`.gitignore`. A URL do banco Python deve continuar apontando para
`localhost:5433/job_market-py`.

Por segurança, a aplicação rejeita outra porta, outro banco e parâmetros de conexão
que poderiam redirecionar silenciosamente para o banco histórico em `5432`.

## Como subir o banco

Inicie somente o PostgreSQL isolado deste projeto e confira o healthcheck:

```powershell
docker compose up -d
docker compose ps
```

## Como executar migration

```powershell
python -m job_collector.main migrate
```

As migrations são incrementais e idempotentes: `001_initial.sql` cria as tabelas e
`002_publication_dates.sql` adiciona `published_date` e `publication_date_source` sem
alterar payloads ou registros históricos.

Depois da migration, preencha os registros existentes usando o `collected_at` de cada
vaga como referência:

```powershell
python -m job_collector.main backfill-publication-dates
```

O comando é idempotente e informa quantos registros TheirStack/SerpApi foram
atualizados, quantos não possuem informação e quantos textos não foram reconhecidos.

## Como coletar

Os defaults são deliberadamente pequenos: cinco vagas e uma página na TheirStack; uma
página na SerpApi. Query, localização e limites padrão ficam centralizados no `.env`;
a CLI expõe somente os limites operacionais úteis para uma execução controlada.

```powershell
python -m job_collector.main theirstack
python -m job_collector.main theirstack --limit 5 --max-pages 1

python -m job_collector.main serpapi
python -m job_collector.main serpapi --max-pages 1

python -m job_collector.main all
```

Para um smoke real deliberado, use uma única tentativa por fonte:

```powershell
python -m job_collector.main theirstack --limit 2 --max-pages 1 --max-retries 0
python -m job_collector.main serpapi --max-pages 1 --max-retries 0
```

Use `python -m job_collector.main --help` e a ajuda de cada subcomando para consultar as
opções disponíveis. Uma chamada ou página pode consumir créditos. Não repita uma coleta
real que já validou o comportamento apenas para obter outra amostra; interrompa em
`401`, `402`, `403` ou `429`.

## Rodada mensal regional

O comando `monthly` organiza uma rodada auditável sem criar outra tabela. Cada
`collection_run` recebe em `query_params` o `round_id`, `collection_kind=monthly`, polo,
estratégia, consulta e localidade realmente utilizada. O catálogo declarativo possui os
oito polos validados nos catálogos oficiais dos fornecedores:

| Polo | ID TheirStack | Origem canônica SerpApi |
| --- | ---: | --- |
| São Paulo | `3448439` | `Sao Paulo,State of Sao Paulo,Brazil` |
| Campinas | `3467865` | `Campinas,State of Sao Paulo,Brazil` |
| São José dos Campos | `3448636` | `Sao Jose dos Campos,State of Sao Paulo,Brazil` |
| Sorocaba | `3447399` | `Sorocaba,State of Sao Paulo,Brazil` |
| Ribeirão Preto | `3451328` | `Ribeirao Preto,State of Sao Paulo,Brazil` |
| Santos | `3449433` | `Santos,State of Sao Paulo,Brazil` |
| Bauru | `3470279` | `Bauru,State of Sao Paulo,Brazil` |
| São José do Rio Preto | `3448639` | `Sao Jose do Rio Preto,State of Sao Paulo,Brazil` |

Os IDs foram confirmados pelo endpoint oficial
`GET https://api.theirstack.com/v0/catalog/locations`; os canonical names, pela
[Locations API oficial da SerpApi](https://serpapi.com/locations-api). O ID `3448433`
representa o estado de São Paulo (`ADM1`) e deliberadamente não é usado como cidade.

Na TheirStack, o ID estruturado seleciona a cidade em `job_location_or`. Na SerpApi,
`location` é a origem regional da pesquisa Google Jobs, não um filtro geográfico rígido.
Em ambos os casos, `raw_jobs.location` continua sendo o valor informado pela vaga e não
é substituído pelo polo pesquisado.

Valide o plano sem abrir banco, criar `collection_runs` ou chamar qualquer API:

```powershell
python -m job_collector.main monthly --round 2026-07 --dry-run
```

A primeira rodada usa oito consultas TheirStack, limite 10 e uma página por polo
(máximo solicitado de 80 itens), e 16 pesquisas SerpApi: duas consultas por polo,
`software engineer` e `desenvolvedor de software`, sempre uma página. Não segue o
`next_page_token`, embora o preserve no banco. A execução real exige autorização
explícita e não solicita confirmação interativa:

```powershell
python -m job_collector.main monthly `
  --round 2026-07 `
  --confirm-live `
  --theirstack-budget 80 `
  --serpapi-budget 16 `
  --max-retries 0
```

Uma assinatura mensal já concluída como `success` é pulada. Uma tentativa `failed` pode
ser refeita; estados `partial` ou `running` são informados e pulados por segurança. A
opção `--force` é excepcional, exige também `--confirm-live` e não deve ser usada na
rodada oficial. Nenhuma execução anterior é apagada.

Após a coleta, o próprio comando gera:

```text
results/monthly/2026-07/
├── summary.json
├── theirstack.json
├── serpapi.json
└── unique_jobs.json
```

Os arquivos por fonte preservam todas as respostas brutas sanitizadas e as vagas
normalizadas com seus relacionamentos. `unique_jobs.json` reduz repetições somente pela
chave `source + external_id`; itens sem ID continuam separados. Coincidências entre as
fontes são apenas sinalizadas por URL pública canonizada ou por título, empresa e
localização normalizados: os registros brutos nunca são mesclados ou removidos.

A meta de 200 vagas é uma referência amostral, não uma garantia. Os limites de crédito
têm precedência, e uma rodada pequena ou concentrada geograficamente não é
automaticamente representativa do mercado paulista.

## Como exportar os resultados existentes

```powershell
python -m job_collector.main export-results
```

Esse comando não chama as APIs. Ele consulta somente `job_market-py`, seleciona a
execução `success` mais recente de cada fonte e substitui:

```text
results/theirstack.json
results/serpapi.json
```

Cada arquivo contém o run, todas as respostas brutas sanitizadas e as vagas normalizadas
com seus IDs de relacionamento. O `raw_payload` não é duplicado nas vagas porque o item
integral já está em `responses`. Os campos `published_date` e
`publication_date_source` também são exportados. Antes da escrita, o sanitizador é
reaplicado e o texto é verificado contra as credenciais locais, sem imprimi-las.

## Paginação, retries e respostas vazias

A TheirStack avança por `offset` e interrompe em lista vazia, página curta, total
conhecido, limite de páginas ou offset repetido. A SerpApi usa somente o token opaco
`serpapi_pagination.next_page_token` e nunca persiste uma URL autenticada. Cada próxima
página só é consultada depois do commit da página anterior.

Falhas de rede e HTTP `500`, `502`, `503` e `504` podem ser repetidos com backoff curto;
outros `4xx` não são repetidos. Toda resposta HTTP transitória é preservada antes da
nova tentativa. Uma resposta SerpApi `Success`/`Fully empty` é sucesso com contadores
zero, não uma falha técnica.

## Como testar

Os testes são unitários, usam fixtures/mocks locais e não acessam APIs nem PostgreSQL:

```powershell
pytest
ruff check .
ruff format --check .
python -m compileall job_collector
pip check
```

## Como inspecionar o banco

Execute o roteiro SQL completo contra o container Python:

```powershell
Get-Content .\scripts\inspect_data.sql -Raw |
  docker compose exec -T postgres psql -U postgres -d job_market-py
```

Também é possível usar DBeaver em `localhost:5433`, banco `job_market-py`, com o usuário
e a senha locais do Compose. Nunca coloque chaves das APIs na conexão ou no editor SQL.

Consultas rápidas:

```sql
SELECT source, status, returned_count, persisted_count, pages_processed
FROM collection_runs
ORDER BY started_at DESC;

SELECT source, COUNT(*) AS total
FROM raw_jobs
GROUP BY source;

SELECT
    source,
    query_params->>'sample_region' AS sample_region,
    query_params->>'query' AS query,
    status,
    returned_count,
    persisted_count
FROM collection_runs
WHERE query_params->>'collection_kind' = 'monthly'
  AND query_params->>'round_id' = '2026-07'
ORDER BY source, sample_region, query;
```

## Segurança

- copie `.env.example` para `.env`, mas nunca versione ou compartilhe o `.env` real;
- os arquivos em `results/` recebem uma segunda sanitização antes da escrita;
- nunca compartilhe ZIP contendo `.env`, `.venv`, caches ou logs;
- os comandos de exportação e inspeção não devem imprimir payloads completos;
- se uma credencial for exposta, revogue-a e faça sua rotação imediatamente;
- `next_page_token`, IDs, descrições, empresas, localizações e links públicos são preservados.

## Limitações e próximos passos

- a retomada automática de uma execução interrompida não está implementada;
- o banco deduplica somente dentro da mesma execução quando há `external_id`; a visão
  mensal única não é uma deduplicação definitiva;
- localização da busca não torna a amostra estatisticamente representativa;
- datas relativas da SerpApi, como `há 18 dias`, permanecem como texto;
- datas derivadas desses textos são estimativas no nível de dia, não timestamps exatos;
- não há análise estatística, dashboard ou agendamento.

Uma etapa futura pode estudar os payloads preservados e definir modelagem analítica sem
alterar a evidência bruta coletada por este MVP.
