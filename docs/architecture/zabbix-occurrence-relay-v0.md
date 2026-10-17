# Intermediário Zabbix → MAESTRO — escopo mínimo V0

**Decisão operacional, 17/10/2026.** Implementação candidata em branch de feature isolada; integração real condicionada à validação de endpoints e contratos, sem autorização implícita para merge ou produção.

## Escopo confirmado pelo operador

Um novo contêiner recebe sinais emitidos pelo detector, verifica a origem e o formato, registra número sequencial + histórico privado e encaminha ao MAESTRO. Não possui IA, lógica de prioridade, organização de fila de trabalho, interface, Redmine/GLPI ou capacidade de investigação/execução.

Esta decisão de escopo prevalece sobre as hipóteses anteriores de multicamadas inteligentes no intermediário documentadas em drafts ILO/MAESTRO/occurrence-intermediary. Não editar unilateralmente os ADRs anteriores; conciliar contratos depois. MAESTRO é destinatário, não Zabbix: a menção a repassar ao Zabbix foi entendida como lapso verbal pelo fluxo estabelecido na conversa.

## Entrada e persistência

Contrato provisório JSON v1 com event_id, occurred_at, asset_ref, signal_code, status e version; referencia service_ref, interface_ref, severity e problem_event_id quando presentes. Token autenticado identifica origem/tenant por configuração local. Replay idêntico retorna mesmo número. Divergência de corpo sob event_id idêntico é recusa. Recovery é registro novo, vinculado ao evento principal; não encerra investigação.

SQLite WAL/FULL somente para recibos, ID numérico e outbox de transporte; não há schema PostgreSQL novo, MySQL, Elasticsearch nem back-end ITSM. Este arquivo é privado e demanda política de backup antes de produção.

## Comunicação / posicionamento

| Iniciador | Destino | Transporte | Necessidade | Autoridade | Zona |
| --- | --- | --- | --- | --- | --- |
| Zabbix homologado | Relay | HTTPS webhook, rota a homologar | Futuro live | token serviço, contrato validado | Zabbix → fronteira relay |
| Relay | MAESTRO | HTTPS URL/porta a descobrir | Futuro live | token serviço, idempotência por ID | Relay → MAESTRO |
| Host AGT local | Relay estágio | HTTP localhost | Somente smoke | token write | loopback host |

Para o estágio: uma conexão à bridge Docker existente, porta publicada **somente no loopback do AGT**. Identidade L2 dedicada: não. L3 dedicado: não demonstrado. IP dedicado: não. IP estático: não. Redes: bridge padrão atual apenas. Sem VLAN extra, sem rotas, sem NAT, sem dual-homing nem exposição pública.

Não confundir o estágio com integração real. **O MAESTRO não foi encontrado como endpoint operacional no AGT**; não inventar URL. O Zabbix existente não foi configurado para emitir eventos ao relay nesta fase. Uma falha de MAESTRO preserva a entrega pendente na outbox; outbox não é fila de priorização.

## Gates

Validar payload, rejeição de duplicados, autenticação, conflitos, concorrência, histórico persistente e perda de destino com testes offline; build/health smoke de contêiner e checks distribuídos por SHA antes de merge. Verificar versão da API Zabbix/action, origem e conectividade efetiva antes de abrir a fronteira. Revisar destino MAESTRO e semântica de ACK antes de habilitar egress.

Live continuity: somente eventos novos quando Zabbix for integrado. Historical acceleration: importação antiga deliberadamente adiada e não pode interferir no fluxo live. Rollback: remover somente contêiner de estágio sem tocar outros serviços.
