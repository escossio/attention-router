# MAESTRO — HTTPS privado sem interromper o ingresso HTTP

Data: 2026-10-17. Estado: CANDIDATO ISOLADO / NÃO IMPLANTADO.
Base: PR #371, SHA 0c80292f6ff4f9fab69dd61860c238efc823c4cd.
Invariantes: arquitetura congelada; não criar outro contêiner, VLAN, macvlan,
IP ou banco por conveniência.

## Diagnóstico

A implementação do PR #368 registrava a rota MAESTRO no mesmo aplicativo
FastAPI de ingresso HTTP legado. Habilitar a flag nesse aplicativo permitiria
tráfego em claro, mesmo com um novo caminho HTTPS em paralelo. O contêiner
atual continua usando o Uvicorn HTTP original e não atende MAESTRO.

## Fronteiras propostas

1. HTTP legado: manter o mesmo aplicativo, porta e handlers preexistentes.
   A rota MAESTRO deixa de ser registrada no aplicativo HTTP.
2. MAESTRO HTTPS: segundo aplicativo ASGI no mesmo processo/container,
   expondo somente POST /api/v1/ingress/maestro/occurrences e GET /health/live.
3. Fail closed: a rota confere o esquema ASGI HTTPS e o runner desliga
   proxy_headers para não confiar em X-Forwarded-Proto do cliente.
4. Listener: endereço IPv4 privado/loopback explícito, sem wildcard,
   porta distinta da HTTP. Exige certificado/chave privados legíveis, chave
   com permissão restrita, sem provisionar novas redes automaticamente.
5. TLS: versão mínima 1.2; cliente Relay valida CA e hostname, nunca
   desliga verificação. Identidade de serviço, audience, binding/tenant
   e escopo seguem no backend MAESTRO. ACK 202/200 somente após commit.
6. Logs: acesso HTTP do listener MAESTRO desativado para evitar vazamento;
   observabilidade resumida precisa ser homologada separadamente.

## Comando candidato, NÃO implantado

python -m attention_router.web.dual_ingress_server

No modo padrão, o runner mantém apenas HTTP e rejeita flag de admissão
MAESTRO ativa sem TLS válido. Variáveis privadas opcionais:
MAESTRO_HTTPS_LISTENER_ENABLED, MAESTRO_HTTPS_BIND_HOST,
MAESTRO_HTTPS_PORT, MAESTRO_HTTPS_CERTFILE, MAESTRO_HTTPS_KEYFILE.
Não acrescentar estas variáveis ao runtime atual nesta etapa.

## Grafo de comunicação

| Origem | Destino | Iniciador | Transporte | Autoridade | Zona |
| --- | --- | --- | --- | --- | --- |
| Clientes atuais | ingresso legado | cliente atual | HTTP sem mudança | contratos existentes | ingress privado |
| Relay V0 | MAESTRO TLS | Relay | HTTPS validado | token escopado, binding, tenant | bridge para ingresso privado |
| MAESTRO | PostgreSQL existente | aplicativo | conexão SQL governada | DB role + recibo | DB privado |

Não existe exigência demonstrada de IP dedicado, novo L2/L3, VLAN ou
IP estático. Verificar endereço e porta viáveis com topologia real,
certificado e DNS antes de implantar.

## Gates

- Confirmar HTTP legado saudável e MAESTRO ausente mesmo com flag ativa.
- Recusar X-Forwarded-Proto falso, HTTP em claro na aplicação MAESTRO,
  certificado inválido, endereço wildcard, portas conflitantes e chave
  com permissões inseguras.
- Testar HTTP e HTTPS simultaneamente por sockets sintéticos loopback:
  CA/SAN, TLS mínimo, demais rotas ausentes, health e shutdown conjunto.
- Testar Relay real contra receptor HTTPS e PostgreSQL descartável:
  rejeição, ACK após commit, replay idempotente, perda de resposta.
- Certificar PostgreSQL em CI03, Python em workers, CodeQL e CI público
  no SHA exato da candidata.
- Homologar consumo HTTP atual, PKI, DNS, ACL de origem, segurança de
  bind, backup Compose, port probe, rollback e SIGTERM antes da janela live.

Nenhum merge, deploy, restart, migração real, mudança MikroTik,
ativação Zabbix real ou credencial de produção nesta etapa.
Live continuity preservada. Historical acceleration/backfill adiado.
