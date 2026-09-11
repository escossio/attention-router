# Architecture Decision Records

Architecture Decision Records (ADRs) são notas curtas sobre decisões que
afetam a arquitetura. Elas registram contexto, escolha e consequências para
que decisões possam ser revisadas sem depender apenas de memória oral.

Status usados neste diretório:

- **Proposed** — proposta em avaliação.
- **Accepted** — decisão adotada para o estado documentado.
- **Superseded** — substituída por outra ADR indicada.
- **Deprecated** — não deve mais orientar mudanças novas.

ADRs descrevem o estado do projeto na data em que foram escritos. Documentos
futuros não são evidência de funcionalidade implementada.

## Índice

- [0001 — Core and adapters](0001-core-and-adapters.md)
- [0002 — Inbound idempotency](0002-inbound-idempotency.md)
- [0003 — Worker claim locking](0003-worker-claim-locking.md)
- [0004 — Immutable policy versioning](0004-immutable-policy-versioning.md)
- [0005 — Transactional outbox](0005-transactional-outbox.md)
- [0006 — Migration strategy](0006-migration-strategy.md)
- [0007 — Control plane and ingress](0007-control-plane-and-ingress.md)
- [0008 — Normalized inbound event](0008-normalized-inbound-event.md)
- [0009 — PostgreSQL integration tests](0009-postgresql-integration-tests.md)
- [0010 — Administrative auth boundary](0010-admin-auth-boundary.md)
- [00011 — Meta WhatsApp inbound adapter](0011-meta-whatsapp-inbound-adapter.md)
- [0012–0019 — Authority, evidence, memory, providers and browser trust](0012-policy-outside-model-authority.md)
- [0020 — Neutral ingress and authenticated tenant binding (proposed)](0020-neutral-ingress-and-authenticated-tenant-binding.md)
