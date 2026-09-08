# Arquitetura

O projeto separa `domain`, `application`, `infrastructure`, `adapters` e `web`.

`domain` contém entidades, políticas e resolução determinística sem FastAPI ou banco.
`application` orquestra criação de interação, decisão, ação, timers e transições.
`infrastructure` contém SQLAlchemy, Alembic, PostgreSQL e worker.
`adapters` define contratos para canal, linguagem e atuadores, hoje implementados por mocks.
`web` expõe API REST `/api/v1` e simulador visual.

O PostgreSQL guarda interações, decisões, tentativas, reconhecimentos, timers, fila e auditoria.
O worker consulta timers vencidos persistidos, então reinício de processo não perde escaladas.

Nesta rodada o núcleo adiciona:

- `inbound_events` para idempotência persistente por `source/external_event_id`.
- `policy_versions` imutáveis, com interação e decisão apontando para a versão exata.
- `outbox_messages` transacional para ações externas futuras.
- claim transacional de timers/outbox com lease simples para recuperação de worker morto.
- journal persistente em `audit_events` com correlation/causation ids.

O fluxo final é:

`Inbound Event -> Normalize -> Deduplicate -> Interaction -> Resolve immutable Policy Version
-> Persist Decision -> Persist Timer/Outbox -> Worker Claim -> Adapter -> State Transition
-> Audit Journal`.

Separação HTTP:

- Control plane: `/api/v1/admin/...`, simulador e diagnósticos, protegido por bearer token local.
- Public ingress futuro: `/api/v1/ingress/...`, hoje apenas sintético e local.

Arquitetura pretendida:

```text
Internet -> future reverse proxy -> PUBLIC INGRESS -> Adapter -> Normalize
  -> Deduplicate -> Core -> Timers/Outbox -> Workers

Operator -> private/local/VPN/restricted network -> CONTROL PLANE
  -> Policies / Audit / Simulator / Diagnostics
```
## Meta WhatsApp Ingress

Fase 4 adiciona o primeiro adapter real somente para inbound:

```text
Internet
  |
Cloudflare HTTPS (router.example.test)
  |
attention-router-ingress (127.0.0.1:18101)
  |
Meta webhook verification/signature
  |
MetaWhatsAppInboundAdapter
  |
NormalizedInboundEvent v1
  |
ActorBinding
  |
Deduplicate
  |
Core -> Timers / Outbox -> Worker
```

O control plane permanece separado em `127.0.0.1:18100` e não existe no listener de ingress.
# Phase 5.1 memory boundary

The persistent actor memory implementation is additive and isolated from decision, autonomy, execution, outbound, Chrome/Xvfb, HA, and internal-ingress semantics. `conversation_messages` is the searchable archive; `memory_claims` is the selectively promoted semantic layer. Historical import is archive/memory-only and must never enter the live inbound pipeline.
