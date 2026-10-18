# AR-1001 — prova de contagens em PostgreSQL descartável

**Data operacional:** 17/10/2026 (horário de Fortaleza). **Executor:** AGT01 como controle, CI03 como worker. **Tipo:** teste sintético sem API, sem dados de produção, rede desabilitada, nenhum deploy.

## Entradas verificadas

- SQL adicional: `ops/observability/failure-catalog/queries/ar-1001-readonly-progress.v0.sql` — SHA-256 `d6380e6830e94a28732637f7194dfc6ecf60dae4abdcd863cfb34397c5064e70`.
- SQL integrado: `ops/observability/failure-catalog/queries/ar-1001-integrated-readonly.v0.sql` — SHA-256 `0a8741a2151c578d973771a2b8d7aa4b3d711e41c9581c44bbd054af9b3626a1`.
- Validador: `ops/observability/failure-catalog/validation/ar1001_disposable_pg.py` (arquivo versionado; uma revisão posterior acrescentou falha explícita de cleanup, sem alterar fixture/consultas). Não apresentar SHA-256 do validador pós-correção como se ele fosse o efetivamente executado antes dela.

O script constrói `postgres:16-alpine` no CI03 já com imagem local, user não-root, rede `none`, sem bind/porta, dados em tmpfs, limite de CPU/memória e capabilities descartadas. Cria exclusivamente tabelas mínimas fictícias, role `ar_readonly_test` com `SELECT`, e dados sintéticos de seis interações diretas e eventos de grupo/antigos que **não** devem entrar na contagem.

## Saídas observadas

| Cenário sintético | SQL adicional | SQL integrado |
| --- | --- | --- |
| 7 eventos diretos em 6 interações | PASS | PASS |
| 6 eventos processados | PASS | PASS |
| 6 decisões no total | PASS | PASS |
| 5 `INSUFFICIENT_CONTEXT/DRY_RUN` | PASS | PASS |
| 1 outbox nova | PASS | PASS |
| 4 janelas liberadas, 1 cancelada | PASS | PASS |
| 7 campos legados preservados por valor esperado | N/A | PASS |
| Role SELECT-only recusa `UPDATE` | PASS | PASS |
| Janela vazia retorna zeros medidos | PASS | PASS |
| Ausência de contêiner residual após teste | PASS | PASS |

Linhas de conclusão observadas para ambos os arquivos: `AR1001_READONLY_ROLE_ASSERT_PASS`, `AR1001_EMPTY_WINDOW_ASSERT_PASS` e `AR1001_DISPOSABLE_PG_PASS`. No integrado houve ainda `AR1001_LEGACY_7_FIELDS_ASSERT_PASS`.

**Não demonstrado:** latência/custo em PostgreSQL operacional, integração host-private `reply_health.py`, compatibilidade com a consulta completa real além das fixtures, criação de itens Zabbix, publicação de contadores reais, respostas Telegram, Relay ou MAESTRO. A tentativa anterior de executar o SQL versionado na base operacional foi bloqueada pela ferramenta remota; não repetir por caminho alternativo para contornar o bloqueio.

## Decisão da fase

**GO para preparar a integração à sonda no repositório e certificar por CI. NO-GO para declararmos sete novos itens live.** Uma ativação requer backup do Zabbix via API e do script privado, prova de performance/compatibilidade no alvo autorizado, rollout da mesma sonda/timer, comparação de valores em três amostras e rollback restrito à sonda/novos itens. O plano detalhado está em [ar-1001-integrated-zabbix-rollout-v0.md](ar-1001-integrated-zabbix-rollout-v0.md).

A evolução do contrato Zabbix→Relay permanece outra etapa. Não criar outro motor nem capturar pacotes automaticamente.
