# AGENTS.md

## Escopo

Este repositório contém a implementação Python simples do MVP de coleta de vagas via
TheirStack e SerpApi. Ele consulta as fontes, registra execuções, preserva respostas
brutas sanitizadas e grava vagas no PostgreSQL para inspeção posterior.

Não implementar aqui análise, pandas, notebooks, NLP, classificação, dashboard,
agendamento, filas, scraping, navegador, autenticação ou publicação de dataset.

## Regras técnicas

- usar Python 3.12+, `httpx.Client` síncrono, `psycopg` e SQL explícito;
- não usar Playwright, ORM, Alembic, Pydantic ou framework web;
- preferir funções pequenas, dicionários e poucos arquivos;
- não criar camadas, interfaces, factories ou dependências sem necessidade concreta;
- usar `argparse` para a CLI, `pytest` para testes unitários e Ruff;
- testes usam mocks/fixtures locais e nunca chamam APIs, Docker ou PostgreSQL;
- manter limites baixos: TheirStack de 1 a 10 itens, 1 a 2 páginas e 0 a 3 retries;
- preservar campos desconhecidos e tokens opacos no payload bruto;
- salvar a resposta sanitizada e confirmar a transação antes de classificar/mapear;
- nunca exibir ou persistir chaves, Authorization, cookies ou URLs autenticadas.

## Isolamento

A versão TypeScript é histórica e não deve ser alterada por tarefas deste projeto. Seu
banco `job_market`, container, volume, migrations e registros também não devem ser
tocados.

Este projeto usa exclusivamente:

- banco `job_market-py`;
- porta externa `5433`;
- container `job_market_postgres_py`;
- volume `postgres_data_py`.

Nunca execute comandos destrutivos contra a pasta TypeScript ou o banco antigo. Não use
`docker compose down -v` como etapa normal.

## Segurança e trabalho

- não ler, imprimir, copiar para logs ou versionar o conteúdo de `.env`;
- manter `.env.example` sem credenciais reais;
- sanitizar recursivamente parâmetros e payloads antes de persistir;
- preservar `next_page_token`, IDs, descrições e demais dados não secretos;
- não versionar respostas reais completas;
- executar chamadas reais somente com autorização explícita, após pytest e Ruff;
- não repetir chamadas bem-sucedidas sem uma razão técnica concreta.

Antes de concluir uma alteração, execute:

```powershell
pytest
ruff check .
ruff format --check .
```

Documente qualquer mudança de comportamento no README sem incluir payloads ou segredos.
