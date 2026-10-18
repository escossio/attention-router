# MGMT — ficha multidomínio AR-1001 (incremento isolado)

**Data:** 18/10/2026 UTC. **Contexto:** complementa a PR #375 (catálogo e coleta Zabbix certificada). **Estado desta branch:** candidato verificável até promoção controlada no painel ativo.

## Motivação e fronteiras

A falha `AR-1001` já tem 15 itens Zabbix com coleta real e o código semântico documentado. A próxima entrega não inventa um novo motor nem muda os triggers: apresenta **a mesma informação ao operador** no MGMT, com um roteiro de troubleshooting por domínios e referências ao modelo OSI. A tela nunca deve afirmar que L1/L2/L3, TCP, HTTP ou OpenTelemetry estão saudáveis simplesmente porque não apareceram anomalias.

**Fontes:** os valores numéricos vêm exclusivamente dos 15 itens `andy.reply.*` do Zabbix ROC, no histórico mais recente de cada item. Os triggers de falha funcional e perda de sonda são lidos, sem alterá-los. Nenhum conteúdo de WhatsApp, `actor_id`, nome de cliente, identificador de pessoa, trace bruto, token ou mensagem é consultado.

## Decisão de implantação

O painel existente em produção é `/srv/projetos/andy-ops-panel`. A cópia viva possui **nove abas** (incluindo INVESTIGADOR, FLOW/E2E e LIVE MESSAGE) que não aparecem integralmente no package `ops/provisioning/andy-ops-panel` da base da PR #375. **É proibido reinstalar o pacote antigo por cima do live**.

O incremento adiciona somente:
- `ops/provisioning/andy-ops-panel/ar1001_observability.py`: query restrita `READ ONLY`, timeout SQL 2s, subprocesso 4s, cache 30s, parser allowlist; não faz nenhuma mutação. Acesso ao DB Zabbix via `docker exec` sob o operador local já autorizado do MGMT.
- `ops/provisioning/andy-ops-panel/ar1001-card.js` e `ar1001-card.css`: aba móvel, cartões numéricos, legenda dos 11 domínios e indicação de dados vencidos; `textContent` para valores, sem HTML arbitrário.
- `ops/provisioning/andy-ops-panel/ar1001_stage.py`: adição pontual e idempotência negativa no `server.py`/`index.html` da instalação **selecionada**. Preserva o JS principal e todos os tabs anteriores. Exige teste prévio em cópia privada do painel.

A nova rota é **GET /api/ar1001**, no mesmo listener, mesma porta, mesmo CSP e mesma fronteira LAN-only. A UI consulta essa rota apenas na aba ativa, até 1 vez a cada 30 s; o cache evita leituras duplicadas. Não existe daemon, thread, timer, contêiner, novo listener, serviço ou acesso de escrita para essa ficha.

## Lógica de classificação

- `probe_ok=1` e dados recentes (até 360s) → `OBSERVED_HEALTHY` **somente para integridade da coleta**, nunca para a aplicação.
- `functional_stalled=1` com coleta íntegra e fresca → `OBSERVED_FAILURE` no **fluxo funcional**.
- `functional_stalled=0` com coleta válida → `NO_RELATED_ANOMALY` para esse detector; não comprova todas as mensagens.
- Ausência/expiração/erro de sonda → `UNKNOWN`, e contadores obsoletos não recebem aparência de métricas atuais.
- L1, L2, L3, TCP, HTTP, runtime de contêiner, PostgreSQL e OTel/Tempo → `NOT_OBSERVED` enquanto não houver prova **vinculada à ocorrência**, mesmo que exista um dashboard geral saudável no MGMT.
- Outbox/entrega → `UNKNOWN` para **obrigação de entrega**, embora a quantidade de saídas do Zabbix seja exibida. `outbox_new=0` não prova sozinho causa ou obrigação.

O contrato retorna o código `AR-1001`, amostragem UTC, idade, cobertura e *reason codes* por domínio. Não há solução ou diagnóstico automático.

## Certificação e rollout

1. Testar parser com fixtures sintéticas: 15 itens íntegros, fonte vencida, valor faltante, entrada maliciosa, trigger inválido, timeout. Validar a preservação de tabs no patcher e JS com `node --check`.
2. Provar GET /api/ar1001 em cópia **privada** dos arquivos realmente instalados. Conferir que 9 tabs anteriores permanecem e app.js é binariamente idêntico.
3. Abrir PR empilhada sobre a PR #375. Não promover com checks ausentes/vermelhos. CI pesada somente distribuída nos workers.
4. Para rollout do painel live, usar backup privado `0700/0600` dos arquivos afetados, preparar cada arquivo como staged, aplicar as alterações somente ao MGMT e reiniciar **apenas** `andy-ops-panel.service`. Validar `/healthz`, `/api/status`, `/api/ar1001`, tab no HTML e histórico Zabbix. Se falhar, restaurar arquivos a partir dos hashes verificados e reiniciar somente o MGMT.
5. Não alterar estados do AR-1001 no Zabbix, nem serviços de coleta, Zabbix, Tempo, PostgreSQL, Telegram, Relay, MAESTRO ou roteamento.

**Continuidade live:** nenhuma mensagem ou sincronização da Andy passa pelo MGMT; falha da ficha não pode bloquear o fluxo. **Aceleração histórica:** intencionalmente não implementada; esta ficha exibe última amostra e janela 6h da sonda, sem importar histórico.

## Estado de maturidade

AR-1001 é o primeiro exemplo. Outras falhas só entram na mesma interface depois de catálogo, medições e razão de confiança certificadas. O perfil não transforma ausência de observação em ausência de problema.
