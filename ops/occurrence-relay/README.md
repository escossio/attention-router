# Occurrence Relay V0 — Zabbix para MAESTRO

Componente mínimo: receber evento autenticado, persistir identificador sequencial com histórico e encaminhar quando a API MAESTRO estiver homologada. Sem Redmine, IA, interface, ranking, fila de prioridade ou executor.

## Contrato de ensaio

POST /v1/zabbix/events, Bearer token restrito vindo de arquivo protegido, JSON de até 16 KiB. A fronteira rejeita requisições com Content-Length duplicado, Transfer-Encoding, Content-Encoding, Content-Type fora de application/json e Authorization duplicado; ambiguidades retornam HTTP 400 e a conexão é encerrada. Sem credencial, retorna HTTP 401. Campos obrigatórios: schema_version=1; event_id; occurred_at com timezone; asset_ref; signal_code; status (PROBLEM, RECOVERY, UPDATE). Opcionais: problem_event_id, service_ref, interface_ref, summary, severity. RECOVERY exige problem_event_id.

Exemplo exclusivamente sintético:

~~~json
{"schema_version":"1","event_id":"synthetic-zbx-001","occurred_at":"2026-10-17T12:00:00Z","asset_ref":"synthetic:router","signal_code":"PORT_DOWN","status":"PROBLEM","interface_ref":"synthetic:ether1","severity":4}
~~~

Retorno 201 para novo evento com occurrence_number numérico; 200 para repetição idêntica; 422 para evento inválido ou mesmo ID com conteúdo conflitante; 401 para autenticação inválida; 503 se o registro não puder ser persistido. Nenhuma falha de entrega ao MAESTRO é confundida com falha de admissão local; outbox pendente sobrevive a restart.

Os campos tenant/source são derivados da configuração confiável do receptor, nunca de texto no evento. SQLite privado é usado somente como diário local/outbox; **não substitui o PostgreSQL** e não introduz MariaDB, Redis ou Elasticsearch.

## Destino futuro

MAESTRO_URL e MAESTRO_TOKEN_FILE não são configurados no estágio. Só habilitar entrega quando confirmar URL e contrato de idempotência, autenticação e ACK durável do MAESTRO. A saída do relay é envelope de estudo (occurrence-relay-v0-draft), não API de produção. A aplicação não cria tickets automaticamente nem interpreta semântica.

## Segurança do ACK e da saída HTTPS (candidato em revisão)

O Relay somente marca a entrega como concluída ao receber do MAESTRO JSON tipado:
`202 ACCEPTED` ou `200 DUPLICATE`, sempre com `receipt_id` não vazio e
`occurrence_number` igual ao enviado. HTTP 2xx genérico, HTML, corpo inválido ou
recibo divergente **não** retira a ocorrência da outbox; redirect permanece bloqueado.

`MAESTRO_CA_FILE` aceita arquivo PEM de CA privada homologada, montado somente-leitura
**apenas quando** o destino `MAESTRO_URL` estiver autorizado. Na ausência dessa
variável, usa raízes padrão do sistema. A verificação de certificado e hostname
permanece obrigatória. Não confiar no certificado snakeoil do Apache existente,
não desativar TLS verification e não habilitar `MAESTRO_URL` nesta fase.

A conexão Apache→ingresso atualmente seria HTTP por rede roteada. Não transmitir
token de serviço real nesse trecho sem criptografia de backend homologada e revisão
da fronteira de confiança. Veja `docs/architecture/relay-maestro-tls-ack-gate-20261017.md`.

## Testes e estágio

Rodar os testes Python dirigidos sem rede externa: `python3 -m unittest tests.test_occurrence_relay_v0 tests.test_occurrence_relay_maestro_ack_v0 -v`. A segunda suíte simula o receptor, sua indisponibilidade, o aceite seguido de perda do ACK, o reenvio idempotente e o restart da store. Ela **não é prova de que o MAESTRO real foi implementado**. Compilar imagem com origem SHA conhecida. Compose de stage publica entrada **só em 127.0.0.1**, sem conexão com Zabbix real ou MAESTRO. Segredo em arquivo 0400 de dono autorizado; bind persistente de dados, UID 10001. Não conectar à rede do Zabbix sem revisão da topologia.

**Live continuity:** futuro webhook deve entregar eventos novos com ACK somente após commit do diário; **historical acceleration:** importação histórica adiada, separada do stream live para preservar cursores/idempotência.

Rollback: remover só container do estágio, preservando dados privados até descarte autorizado. Não alterar VLANs, MikroTik, contêineres ou PostgreSQL operacional.
