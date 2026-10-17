# MAESTRO — receptor mínimo e persistência do Relay V0

**Data:** 2026-10-17.
**Estado:** CANDIDATO EM PR DRAFT. Não implantado. Sem merge, endpoint ativo, binding operacional ou migrations em banco real. PR #368, continuidade de contrato puro do SHA 5a39350.

## Descobertas e decisão de reuso

O Attention Router já oferece PostgreSQL, migrations Alembic, credenciais de integração e bindings de tenant, além de ingresso HTTP em web/ingress_app.py. O sistema tem infrastructure/models.py, integration_admission e integration_inbox. A última armazena **somente** contract_type=inbound_event, impõe schema e uma semântica própria de integração; o Relay transmite occurrence-relay-v0-draft, com cabeçalho Idempotency-Key. Adaptar silenciosamente ao formato genérico mudaria semântica de timestamps e identidade; reutilização direta da tabela/rota seria errada.

**Solução mínima:** reaproveitar o processo de ingress, o mesmo PostgreSQL, as tabelas de credenciais e tenants e a disciplina de locks, com **uma tabela adicional e estritamente específica de recibos de transporte do MAESTRO**. Isso NÃO cria contêiner, sistema de chamados, base PostgreSQL adicional, interface, prioridade, execução, chamado novo ou duplicação do histórico do detector. O armazenamento adicional existe para não alterar a tabela do ingresso genérico.

## Artefatos propostos

- attention_router/maestro/relay_contract.py: valida envelope após identificação de serviço.
- attention_router/maestro/admission.py: valida token e binding sob transação PostgreSQL, usa ordem de bloqueio tenant → binding → credencial, checa escopo dedicado maestro:relay:write e audience maestro-relay-ingress; insere recibo e só devolve ACCEPTED após o commit. Replays idênticos resultam em DUPLICATE; conteúdo divergente dá CONFLICT.
- attention_router/infrastructure/models.py: MaestroRelayReceiptRow, identidade única por (tenant, source_system, instance_id, occurrence_number), digest canônico e referência do sinal. A linha é recibo histórico, **não** ticket, fila ou disparo de investigação.
- alembic/versions/0058_maestro_relay_receipts.py: nova tabela em PostgreSQL atual com índices, integridade referencial e constraints; migration somente versionada, não aplicada no AGT. Downgrade bloqueado se houver qualquer recibo, exigindo exportação e decisão de retenção antes da remoção do histórico.
- attention_router/web/maestro_relay_ingress.py: POST /api/v1/ingress/maestro/occurrences, no app de ingresso existente, com validação de headers HTTP, corpo limitado, autorização obrigatória e ausência de mensagens sensíveis nos erros.
- attention_router/config.py: flag maestro_relay_ingress_enabled default FALSE; sem credencial real/binding provisionado o serviço continua NO-GO.
- tests/test_maestro_relay_contract_v0.py e tests/test_maestro_relay_http_v0.py: testes leves offline de schema e rota default-off.
- tests/integration/test_postgres_maestro_relay_ingress.py: testes de commit real no banco descartável PostgreSQL dos workers, casos de replay, conflito, revogação, cross-tenant e concorrência.

### Contrato da admissão

Credencial Bearer provisionada por mecanismo já existente, mas **escopo diferente** de integration:inbound_event:write. O token nunca vem do evento nem concede tenant automaticamente. O binding canônico deve ser CAPABILITY + ops.zabbix_relay + instance_id e audience maestro-relay-ingress. O MAESTRO valida tenant/source/ID enviados **contra** esse binding, sem confiar nos campos como autoridade.

A chave local zabbix:N é permitida somente no âmbito de um binding homologado. A unicidade persistente usa (tenant, source_system, instance_id, N), impedindo colisão entre instalações independentes. O recibo após commit inclui receipt_id e occurrence_number; duplicata legítima recebe o mesmo receipt_id. Uma falha de PostgreSQL nunca confirma 2xx. Evento RECOVERY cria outro recibo vinculado a problem_event_id sem fechar incidente. Uma admissão não concede poderes de remediação.

**Estados HTTP candidatos:** 202 ACCEPTED, 200 DUPLICATE, 401 UNAUTHENTICATED, 403 FORBIDDEN, 409 CONFLICT, 422 INVALID_CONTRACT, 503 UNAVAILABLE. Erros de framing 400/413/415. Sem endpoint MAESTRO ativado/roteado externamente e sem autenticação de produção.

### Grafo e confiança

| Origem | Destino | Iniciador | Transporte | Fase | Autoridade | Zona |
| --- | --- | --- | --- | --- | --- | --- |
| Relay V0 | Runtime ingress (MAESTRO route) | Relay | HTTPS validado | Futuro | credencial de serviço e audience/binding/escopo restritos | Relay → ingress interno |
| Ingress existente | PostgreSQL existente | Ingress | conexão SQL existente | Quando ativado | role de DB já governado, migration homologada | Aplicação → DB privado |

Não adicionar rede, VLAN, bridge, IP dedicado, IP estático, roteamento nem contêiner por efeito deste PR. L2 dedicado: não; L3 dedicado: não demonstrado; IP dedicado/estático: não. A implantação precisa validar alcance TLS entre o Relay e a API já existente sem criar trânsito inesperado. Rollback: desabilitar a flag (default), sem trocar a rede; migração nunca deve ser revertida em produção sem política de preservação de recibos.

## Gates explícitos

- Testes offline no AGT: apenas suites focadas de baixa carga, sem DB real.
- CI distribuído PostgreSQL deve testar no banco criado e destruído exclusivamente pelo fixture; não correr full suite/migrations no AGT.
- Não publicar credenciais em repositório nem ativar envio do Relay antes de testes autenticados ponta a ponta, validação da identidade TLS, provisionamento da credencial e checklist de retenção/backup.
- Tráfego existente Zabbix/Telegram e estágio Relay continuam intactos.
- Live continuity: webhook real do Zabbix segue inalterado. Historical acceleration: nenhum backfill, cursors intocados; ativação não retroalimenta backlog histórico.
- Ainda falta acoplar a **orquestração futura MAESTRO** aos recibos. Aceitar evento não equivale a despachar Investigador, priorizar ou resolver caso.

**CI npm:** foi transportada sem modificação a correção certificada do PR #360 para o teste de SDK (compatibilidade npm 10/12); não modifica o comportamento do produto e mantém as asserções.
