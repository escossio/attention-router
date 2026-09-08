# ADR 0001: Núcleo e Adaptadores

## Status

Aceita.

## Decisão

Manter Meta, WhatsApp, Home Assistant, Android, TTS e IA fora do núcleo. O motor expõe contratos
`ChannelAdapter`, `LanguageAdapter` e `ActionAdapter`; nesta versão, todos usam mocks
determinísticos.

## Consequências

O núcleo já persiste decisões, timers, ações e auditoria sem dependência de provedores. Adaptadores
reais poderão ser ligados depois sem reescrever a máquina de estados.

