# ADR 0008: Contrato NormalizedInboundEvent

## Status

Aceita.

## Decisão

Adapters externos implementam `InboundAdapter.normalize(raw_event) -> NormalizedInboundEvent`.
O core consome apenas o evento normalizado schema `1`.

## Consequências

Meta, WhatsApp ou outro provedor futuro não contaminam o core com payload específico. Breaking
changes no contrato interno exigem novo `schema_version`.

