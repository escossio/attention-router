# Flow Map V1 — bridge segregada e mapa Grafana

Esta fatia é um **serviço independente e somente-leitura** na rede Docker privada
`andy-roc-monitoring`; não expõe porta no host. Não modifica o Reader Tempo,
a Andy, o runtime WhatsApp, o TCP Brain ou o Zabbix Agent2.

## Fonte de verdade e identidade

- Zabbix ROC → histórico `history_text` com clock de coleta, usando a visão
  SQL `roc_flow_map_v1_inventory`, que sanitiza e restringe os registros a:
  `andy-whatsapp-runtime/transport/1`,
  `attention-router/ingress/1` e
  `attention-router-live-flow-stage4/worker/1`.
- O banco deve expor apenas SELECT nessa visão a um role PostgreSQL
  independente `roc_flow_map_v1_reader` com LOGIN/NOSUPERUSER/NOCREATEDB/NOCREATEROLE,
  sem privilégios sobre as tabelas Zabbix. Nunca dar acesso de Zabbix Admin.
- O segredo Postgres permanece **fora do Git** em
  `/etc/andy/flow-map/zabbix_pgpass`, UID 18089, mode 0400. É montado
  read-only no container em `/run/secrets/zabbix_pgpass`; não vai para env.
- Tempo → Reader Python reaproveitado, trace selecionado com limite 24h.
- A regra `UNKNOWN` prevalece se o dado Zabbix está velho/faltando.
- Falha do Tempo não destrói o esqueleto Zabbix: o mapa conserva os nós,
  sinaliza `tempo=UNAVAILABLE` e deixa a participação `UNSELECTED`.
  Falha do próprio Zabbix responde 503 em vez de criar saúde sintética.
- Rede TCP Brain não é ligada automaticamente: V1 informa somente ligações
  declaradas e OTel parent/child / SPAN_LINK realmente observado. Relação
  física sem testemunha continua `EXPECTED_ONLY`.
- `map/adapter.py` exporta apenas metadados explícitos. Mensagem, endereço
  de remetente WhatsApp, conteúdo do span e dados de conexão não são enviados.

## Deploy previsto (após CI)

1. Validar o SQL com `BEGIN; [view]; SELECT ...; ROLLBACK;` sem persistir
   schema. Verificar as três entidades e as interfaces atuais.
2. Fazer backup privado do estado de ACL/role/view atual antes da operação.
   Criar o role DB restrito e a visão versionada, com GRANT SELECT somente
   ao role. Testar que SELECT em `items` e `history_text` é negado.
3. Criar PG passfile fora de Git, legível exclusivamente para UID 65534
   do container. Jamais imprimir senha ou conteúdo do passfile.
4. Build e start apenas `andy-roc-flow-map` com Compose separado. Testar
   `/health`, `/map?trace_id=latest`, entradas inválidas e falha de origem.
5. Grafana Infinity 4.0.0: acrescentar `http://roc-flow-map:8080`
   em allowedHosts do datasource `roc-flow`, preservando o Reader.
   Aplicar reload da provisioning por API autenticada, sem reiniciar Grafana.
6. Publicar **novo** dashboard `roc-live-message-path-v1` no provisioning,
   preservando o dashboard ROC-E2E anterior. Validar render real com trace
   latest e trace_id explícito, e atualizar evidência na PR.
7. Rollback: retirar somente novo dashboard, remover allowedHost adicionado,
   reload, stop do sidecar e revogar SELECT do role. Não remover histórico
   do Zabbix. Um rollback de DB view/role deve ser transacional e autorizado.

## Limites

- Nenhum envio outbound, nenhuma captura de body, nenhum novo endpoint público.
- A aplicação nunca deve usar Zabbix Admin, Docker socket ou credenciais
  de acesso ao banco da Andy. PostgreSQL no monitoring é usado exclusivamente
  via a visão sanitizada para essas três entidades.
- O Node Graph apresenta estados e causalidade instrumentada, não uma
  animação temporal de pacotes. A camada de animação pode ser avaliada depois.
