# AR-1001 — proposta de enriquecimento no coletor existente

**Data:** 17/10/2026. **Fase:** homologação offline no CI03 concluída; rollout Zabbix ainda **não realizado**. Na próxima passada de implantação, não reconstruir o sistema.

## Decisão após prova PostgreSQL descartável

Foram comparadas duas candidatas:

- `queries/ar-1001-readonly-progress.v0.sql`: consulta adicional de sete campos. **PASS** em banco descartável com usuários e dados sintéticos.
- `queries/ar-1001-integrated-readonly.v0.sql`: consulta **única**, preservando as **sete primeiras colunas** originais do `reply_health.py` e acrescentando as sete métricas tipadas. **PASS** em banco descartável, incluindo contagem de duplicatas, grupos excluídos, três tipos de estado e teste de role SELECT-only.

**Recomendação de implantação: integrada**, pois não acrescenta um segundo ciclo SQL/sonda/timer; o custo de consulta em PostgreSQL real ainda deve ser medido, e a identidade semântica das sete métricas anteriores deve ser atestada contra golden fixtures antes do rollout. Números de teste: originais 6/5/5/0 e timestamps positivos; novos 7/6/6/5/1/4/1. São fixtures **sintéticas**, não resultados da Andy.

O teste `validation/ar1001_disposable_pg.py` foi executado no worker CI03 com `postgres:16-alpine` existente, `--network none`, sem porta publicada, armazenamento em tmpfs, usuário não-root, recursos limitados e role `ar_readonly_test` com SELECT. Os contêineres foram removidos pelo validador. **Nenhum banco AGT foi escrito.**

## Contrato dos itens novos

Fonte de verdade: `zabbix-items.ar-1001.v0.json`. São sete itens numéricos sem sinal de causa raiz. **Não criar trigger nem alerta nesta primeira ativação**; antes, validar a qualidade dos dados medidos e não degradar os dois triggers já ativos da Andy. Como o evento de PROBLEM está aberto desde 15/10, a mera criação dos novos itens não dispara novo evento histórico.

A função pura `ops/observability/failure_catalog/ar1001_metric_projection.py` valida saída psql delimitada por sete barras originais + sete métricas adicionais, impede negativos, lixo, saída duplicada, incoerência nas contagens e vazamento de payload em exceções. **Não inicia processos, consulta bancos nem publica métricas por si.** Para ativá-la, a sonda host-private já existente deverá chamá-la no mesmo ciclo do timer; a sonda permanece responsável por `next_state()`, `probe_ok`, `zabbix_sender` e rollback.

## Checklist específico antes do Zabbix ativo

1. Exportar por API do ROC os 8 itens e 2 triggers atuais, as definições do host lógico, a amostra recente de `history_uint` e informações de retorno. Backup privado com retenção e hash, sem imprimir detalhes de credenciais.
2. Fazer cópia privada verificável de `reply_health.py` e unidade/timer originais. Confirmar checks da PR no SHA completo, estado do timer e último `zabbix_sender` ACK.
3. Verificar custo e sintaxe da consulta integrada na base real com escopo estritamente read-only, declaração de statement timeout e sem lock de escrita, respeitando gates/limites operacionais. Se houver bloqueio de segurança/ferramenta, **parar sem contornar**.
4. Provisionar por **API oficial Zabbix** apenas os 7 itens trapper do manifesto, no host lógico já existente, sem alterar triggers/media/action.
5. Substituir **somente a consulta no mesmo coletor existente**, integrando parser projetado e mantendo os 8 itens antigos. Nenhum novo process/scheduler. Verificar mapeamento das 7 colunas legadas, persistência do estado e operação fail-closed; se resultado inválido, publicar somente `probe_ok=0`.
6. Observar ao menos três amostras com timestamps novos, comparar contadores entre PostgreSQL e histórico Zabbix, e confirmar que **não** mudou o estado `ANDY_WA_AUTO_REPLY_STALLED` por efeito da implantação.
7. Em caso de divergência, restaurar apenas o script host-private e desabilitar os novos itens (preferível preservar os registros/histórico); não reiniciar o banco ou Relay nem forçar recovery.

**Toda falha continua representada por código AR-1001 e evidência de cada domínio, não por solução automática.** O Zabbix será atualizado antes de qualquer novo contrato Relay/MAESTRO.

## Testes, limites e referências

`python3 -m unittest tests.test_operational_ar1001_projection tests.test_operational_ar1001_readonly_probe_v0` testa o mapeamento, contagens incoerentes e guardas de SQL sem conectar no banco. `validation/ar1001_disposable_pg.py` exige Docker PG no worker designado e executa schema/fixture exclusivamente sintético; **não** faz parte dos testes Python genéricos de CI. A investigação da sonda existente e da sua semântica está em [ar-1001-source-audit-20261017.md](ar-1001-source-audit-20261017.md).
