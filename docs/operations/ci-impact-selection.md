# CI seletivo por impacto — GitHub Actions e publicadores distribuídos

## Objetivo

Evitar testes e builds pesados sem pertinência ao diff, **sem enfraquecer a proteção de `main`**. A proteção continua exigindo exatamente os mesmos oito checks. Nenhum status é publicado como prova de teste realizado quando a suíte foi dispensada. As decisões de não aplicabilidade são explícitas, justificadas e vinculadas ao SHA do PR ou ao intervalo de push.

A política única está em `ops/provisioning/github-app-control-plane/ci_impact_policy.py` e é consumida por:

- `scripts/ci_impact_report.py` no `Public CI`. No PR, o arquivo é carregado do SHA da *base confiável*, não do conteúdo proposto pelo autor do PR. Para `push main`, o cálculo considera o intervalo `before...after`. O estágio gera outputs para `python-tests`, `transport-tests`, `docker-build` e `postgres`.
- `distributed_shadow_checks.py` e `distributed_postgres_check.py` da GitHub App instalada no AGT01. Eles conferem o SHA exato e consultam todas as páginas de arquivos alterados antes de decidir sobre Python, Transport, Docker ou PostgreSQL. Para `escossio/andy-android`, o contrato existente permanece inalterado.

## Regra de seleção (conservadora)

| Mudança comprovada | Suítes que exigem execução |
|---|---|
| `STATUS.md`, documentação/ativos não executáveis em `docs/` | Nenhuma suíte pesada |
| `README.md` na raiz | Build Docker (arquivo copiado para imagem) |
| JS cognitivo público com teste de sintaxe | Transport |
| Testes unitários Python | Python |
| Testes de integração | Python + PostgreSQL |
| Código/fixtures do Transport | Transport + Docker (testes isolados do Transport só Transport) |
| Composição e runtime WhatsApp | Python + Transport + Docker |
| Alembic e migrations | Python + PostgreSQL + Docker |
| Código `attention_router/`, CI, políticas, provisionamento, dependências, caminho desconhecido | **Todas** as suítes |

O `secret-scan` e as duas análises CodeQL continuam rodando em cada PR. `Deploy Pages` dispara em `main` apenas quando os arquivos montados pelo site ou o workflow Pages mudam, mantendo `workflow_dispatch` manual. `CI Agent` continua independente e não sofre alteração nesta versão.

## Evidência e falha segura

- `NOT_APPLICABLE` é exibido no título/resumo do check distribuído, com motivo, quantidade de arquivos e hash dos caminhos. O status requerido permanece concluído, sem consumir worker; não é um resultado `pytest PASS`.
- Nos jobs públicos, o primeiro passo registra a dispensa e **não executa** instalação de dependências, testes nem builds. Em caso de erro do classificador, outputs ausentes ou diff desconhecido, os jobs rodam integralmente.
- PR sem lista completa de arquivos, truncamento da API do GitHub, renomeação sem origem, HEAD divergente, branch desconhecida, erro HTTP, retorno inesperado ou arquivo não classificado resultam em `FAIL_CLOSED` e execução completa.
- A API de PR é consultada usando a identidade atual, base `main`, SHA exato e contagem de arquivos. O limite de 3000 arquivos do GitHub é tratado como incompleto. Para push, comparações não lineares ou com 300 ou mais arquivos exigem a suíte completa.
- A PR inicial de implantação desta política é um caso de **bootstrap**: como sua base ainda não contém o classificador, o Public CI roda todas as suítes. O cutover dos publicadores instalados no AGT01 ocorrerá somente após merge certificado.

## Deploy dos publicadores do AGT01 (etapa operacional separada)

Os arquivos fonte instalados em `/usr/local/lib/andy-github-app` devem corresponder **byte a byte** aos do SHA de `main` certificado. Na mesma janela, incluir o novo módulo `ci_impact_policy.py` nos dois publicadores. Verificar ausência de jobs em andamento, backup dos três arquivos, checagem de import/pytest e então reiniciar os dois serviços controladamente; comparar estados e checks. Se ocorrer erro de importação, restaurar arquivos e processos originais. O webhook e o banco da fila não devem ser modificados. O container OCI candidato também foi atualizado para incluir o novo módulo.

Não fazer cutover com fila ou job em execução, não alterar ruleset/required contexts, não fabricar checks e não publicar `NOT_APPLICABLE` quando faltarem evidências.

## Validação

Os testes locais `tests/test_ci_impact_policy.py`, `tests/test_ci_impact_publishers.py` e os testes existentes dos publicadores demonstram escopo, renomeação, paginação, indisponibilidade da API, proteção de checks e ausência de execução quando comprovadamente não aplicável. A PR da política executará obrigatoriamente a certificação completa, inclusive PostgreSQL distribuído, porque altera infraestrutura de CI.
