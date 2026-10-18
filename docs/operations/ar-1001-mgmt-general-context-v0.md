# MGMT AR-1001 — contexto geral OSI e Tempo, sem inferência causal

**Fase:** 18/10/2026. **Natureza:** incremento do painel existente, **não** outro motor, coletor, alarme ou contrato de transporte.

## Por que complementar a ficha

A [primeira ficha AR-1001](ar-1001-mgmt-evidence-card-v0.md) já consulta 15 métricas do Zabbix e exibe 11 domínios de troubleshooting. O operador também precisa enxergar o panorama das ferramentas MGMT disponíveis **sem transformar uma medição geral em prova de que a ocorrência percorreu tal caminho**.

A versão acrescenta uma seção separada, **"Contexto geral — NETWORK/OSI e Tempo"**, alimentada por *itens Zabbix já existentes* e lidos na **mesma transação somente-leitura** da ficha. Não cria novo polling contínuo nem toca no Zabbix. Continua usando timeout SQL de 2 s, subprocesso 4 s, cache de 30 s e somente a aba ativa solicita atualizações.

## Inventário de itens reutilizados

| Item do Zabbix ROC | Significado verificável | Limite |
| --- | --- | --- |
| `andy.summary.domains` | Número de domínios de rede observados pelo painel Network/OSI | Não descreve cada salto do trajeto da mensagem |
| `andy.summary.reachable` | Número de domínios alcançáveis naquela coleta | 8/8 não significa WhatsApp funcional |
| `andy.summary.complete` | Número de vizinhanças completas | Não é estado TCP |
| `andy.summary.incomplete` | Número de vizinhanças incompletas | Não implica causalidade do AR-1001 |
| `andy.topology.consistent` | Consistência calculada da topologia, **caso haja dado numérico recente** | Sem histórico válido permanece NÃO OBSERVADO |
| `andy.tempo.query_ok` | Disponibilidade da API de consulta Tempo para a sonda existente | Não prova ingestão de spans ou correspondência com uma interação |
| `andy.tempo.log_probe_ok` | Inspeção de logs do Tempo pelo coletor existente | Falha desta sonda não comprova indisponibilidade do Tempo |

A fonte única continua sendo o banco do **Zabbix ROC** em leitura; os dados não são extraídos de logs privados por essa ficha. A presença de Collector/Tempo/Docker não autoriza atribuir estado aos domínios do incidente.

## Semântica e isolamento

A resposta `GET /api/ar1001` continua trazendo `metrics` (15 itens AR-1001) e `facets` (11 domínios específicos da ocorrência). Acrescenta **`general_context`**:

- `scope=INFRASTRUCTURE_GENERAL_NOT_INCIDENT_CORRELATED`
- `incident_correlated=false` (invariante).
- `sensors`: somente as sete chaves allowlist quando numéricas válidas, com UTC, idade e `fresh`.
- `assessments`: integridade do panorama de domínios Network/OSI, da API do Tempo e da sonda de inspeção dos logs.

Uma amostra expirada, ausente ou de formato inválido vira `UNKNOWN`. Uma chave duplicada ou inesperada invalida o **contexto geral**, mas não apaga um sinal funcional AR-1001 legítimo. A fonte de contexto **não altera** os estados `NOT_OBSERVED` de rede física, L2, L3, TCP, HTTP ou tracing na ocorrência. Para promovê-los será necessária medição **correlacionada no mesmo alvo e janela**, com proveniência verificável.

### Observação real na homologação read-only

O Zabbix apresentou domínios **8/8 alcançáveis**, vizinhanças **8/8 completas** e nenhuma incompleta. No Tempo, a sonda `andy.tempo.query_ok=1` respondeu, mas `andy.tempo.log_probe_ok=0` indicou **falha da inspeção dos logs**. São dados de contexto que poderão mudar; **não demonstram causa raiz** de a Andy não responder. A checagem de consistência da topologia não possuía amostra `history_uint` válida na janela examinada; deve aparecer ausente, nunca zero presumido.

## Teste, rollout e rollback

- Testes Python locais cobrem 14 cenários, incluindo contexto ausente, expirado, origem duplicada, campo inesperado, contadores inconsistentes e invariância da falha AR-1001.
- Smoke HTTP de cópia isolada do MGMT instalado (10 abas): `/api/status`, `/api/ar1001`, `/index.html`, JS/CSS HTTP 200; 15 indicadores de ocorrência, 11 facetas e 6 fontes gerais válidas disponíveis.
- A aplicação futura mexerá **somente** nos arquivos `ar1001_observability.py`, `ar1001-card.js` e `ar1001-card.css` da instalação MGMT atual, após checks de PR e backup privado byte a byte. **Sem** substituir `index.html`, `server.py`, `app.js` ou `styles.css`.
- Reiniciar, se necessário, **apenas `andy-ops-panel.service`**, com rollback dos três arquivos específicos caso falhe o smoke. Não alterar Zabbix/Tempo/Collector/Relay/MAESTRO.
- **Live continuity:** nenhuma mensagem depende da ficha. **Historical acceleration:** não alterada; sem replay nem importação.

A partir daqui, a próxima investigação de lacunas será a ligação comprovada entre `interaction_id`, event/time window e trace/span ou item OSI correspondente, sem payload livre ou IPs privados versionados.
