# MGMT — mapa canônico de observabilidade da Andy (V0)

**Status:** inventário arquitetural e de evidências, 17/10/2026. **Não é uma implantação nem um novo motor.**

## Terminologia obrigatória nas próximas sessões

Quando o operador disser **MGMT**, **aba MGMT** ou **plataforma MGMT**, está se referindo ao **conjunto de observabilidade e gerência da infraestrutura da Andy**, não apenas ao servidor web denominado *Andy Ops Live Supervisor*. Essa arquitetura reúne painéis, coletores, telemetria, inventário, logs, rastreamento de mensagens e CI. O painel pode incorporar outras UIs por iframe ou links; isso não faz dos dados uma única fonte.

Antes de mudar o Zabbix, diagnosticar falhas ou preparar o MAESTRO, **consultar este documento** e o [contrato de evidências](zabbix-layered-evidence-v0.md). Não criar outro motor de coleta só porque a informação não está no contexto de um chat. Confirmar no runtime se cada componente está ativo e coletando o sinal específico desejado.

## Camadas e fontes existentes

| Domínio de troubleshooting | Recurso ou fonte existente | Evidência que pode fornecer | Proveniência / limites |
| --- | --- | --- | --- |
| Enlace físico (OSI L1) | MikroTik SNMPv3, Zabbix ROC, NETWORK / OSI | Interface administrativa/operacional, contadores e frescor de sondas | `ops/observability/network-osi/README.md`; estado de link não prova aplicação |
| Domínio Ethernet/VLAN (OSI L2) | NETWORK / OSI, snapshot de topologia e ARP | VLAN-parent, vizinhança e inconsistência L2 | A ligação física/topologia precisa ser verificada |
| IP/rede (OSI L3) | ICMP nativo Zabbix, tabelas OSI | Alcançabilidade, perda, tempo de resposta, dependências | ICMP não equivale a TCP/HTTP saudável |
| TCP/transporte (OSI L4) | Dados de socket quando instrumentados, OTel quando aplicável, coleta manual orientada | Timeout, conexão e retransmissão caso exista medição | **Não** existe coletor tcpdump contínuo certificado por este documento |
| Contêiner/host | Docker Inventory via Agent2, Dozzle, Compute | Running, restart, healthcheck, CPU e logs de processos | HEALTHY não prova resposta de negócio |
| HTTP/API (OSI L7) | GoAccess, access logs, spans e métricas | Rota, método, HTTP code, latência por contexto | HTTP 200 no health não prova resposta real |
| Tracing distribuído (transversal) | OpenTelemetry Collector, Tempo, Grafana ROC | Spans, erros tipados, durações, rastros correlacionados quando instrumentados | Ausência de span não prova falha nem integridade |
| Fluxo da Andy (OSI L7) | AGENT / TRANSPORT e trace de mensagens do Andy Ops | Ingresso, processamento, decisão, intent, outbox e entrega | Sensores opcionais devem ser verificados; não exibir mensagens/PII |
| Banco/estado (OSI L7/infra) | PostgreSQL e métricas/tracing já existentes | Estado e latências, bloqueios quando instrumentados | Não confundir consulta manual com medição Zabbix pronta |
| Relay/MAESTRO | Zabbix, Relay, MAESTRO/recibos | Emissão, persistência, retry, ACK e estado de transporte | Não comprova investigação ou resolução |
| Disponibilidade do coletor | Zabbix, OTel, MGMT | Coleta ativa, último dado válido, retenção e UNKNOWN | Coletor indisponível != aplicação saudável |

O projeto já possui `L1_PARENT_DOWN`, `L2_DOMAIN_DOWN`, `L2_TOPOLOGY_MISMATCH`, `L3_NEIGHBOR_INCOMPLETE` e `L3_ENDPOINT_UNREACHABLE`; sua hierarquia de dependências precisa ser preservada para não alarmar em cascata com causas supostas.

## Ferramentas que formam MGMT

**Andy Ops Live Supervisor:** COMPUTE (AGT/CI01/CI02/CI03), CHAT / CONSOLE, AGENT / TRANSPORT, HTTP / GOACCESS, CONTAINERS / DOZZLE, NETWORK / OSI e rastreamento limitado de mensagens. Na evolução em PR #364, foi adicionada a aba INVESTIGADOR; implantação e estado de leitura devem ser certificados separadamente. Fontes: `ops/provisioning/andy-ops-panel/`.

**Observabilidade ROC:** Zabbix containerizado e Agent2 Docker Inventory; MikroTik via SNMPv3; dashboard NETWORK / OSI; Collector OTel, Tempo, Grafana, Flow Map, GoAccess, Dozzle e ferramentas de auditoria. São origens e visualizações distintas, com retenções, atrasos e identidades próprias. Fontes: `ops/observability/`, `docs/observability/`.

**Não inventar cobertura.** Um painel instalado pode estar sem sensores, sem permissões, sem dados recentes ou sem a mesma janela do incidente; o diagnóstico deve registrar `NOT_OBSERVED` ou `UNKNOWN` conforme o caso. O fato de uma tela ter uma aba não é prova de dados naquela camada.

## Fluxo desejado (preparo por camadas)

1. **Zabbix** observa fatos, classifica condição de alarme e reúne **campos tipados e referências** das fontes já existentes. Primeiro definir cobertura e frescor, depois incrementar itens e tags por evento.
2. **Relay** recebe somente o contrato vigente, confere integridade e entrega com idempotência. O formato atual é estrito: não anexar campos adicionais antes de nova versão homologada.
3. **MAESTRO** confirma recibo e, futuramente, poderá usar essas referências durante investigação. O catálogo de falhas não decide causa nem remediação.

A evidência poderá apontar para Grafana/Tempo/GoAccess/Zabbix/MGMT quando o escopo/autorização permitir. Não copiar URLs privadas, dados brutos, payloads, token, IPAM, inventário de clientes ou logs completos para o GitHub, Telegram ou Relay.

## Como usar em novas sessões de Codex

- Ler este arquivo, `docs/architecture/zabbix-layered-evidence-v0.md`, `docs/operations/operational-failure-catalog-v0.md` e `ops/observability/failure-catalog/`.
- Descobrir quais sensores existem e distinguir `live observado`, `código versionado`, `proposta` e `não coletado`.
- Para nova falha, reutilizar código AR-XXXX ou criar nova entrada mediante evidência; preservar fonte, timestamp, escopo, dependências e qualidade de coleta.
- Evoluir **primeiro o contrato Zabbix**, testar dados sintéticos e lacunas; modificar o Relay/MAESTRO somente por decisão separada de compatibilidade.
- Não executar tcpdump/pcap em tempo real, ativar mídia ou alterar triggers apenas por leitura deste documento.

**Política operacional da sessão:** priorizar serviço e evidência útil. Não impor firewall novo, mTLS, VLAN nova ou arquitetura extra para completar um catálogo documental. Autorização e integridade de ingress/credenciais existentes permanecem.
