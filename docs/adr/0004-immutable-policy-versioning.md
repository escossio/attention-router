# ADR 0004: Versionamento Imutável De Políticas

## Status

Aceita.

## Decisão

`policies` aponta para a versão ativa e `policy_versions` guarda a configuração completa com
checksum. Edição cria nova versão; versões históricas não são alteradas.

## Consequências

Decisões antigas permanecem reproduzíveis porque `interactions` e `decisions` registram
`policy_version_id`.

