# Eligibility policy

Candidates pass through `PROMOTE`, `ARCHIVE_ONLY`, `BLOCK`, or `LOW_CONFIDENCE`.

Stable explicit statements such as a self-reported name or employer may be promoted. Transient statements remain archive-only. Secrets and credentials are blocked and their raw text is not searchable. Third-party statements retain a lower-confidence source quality and must not be treated as self-reported facts. Inferences are never silently stored as direct facts.

Source quality order is `OPERATOR_CONFIRMED`, `SELF_REPORTED`, direct evidence, contact metadata, third-party statement, inference. This V0 records the quality on the promoted claim and keeps temporal history rather than overwriting claims.
# V0 incremental

Promovemos somente fatos estáveis ou explicitamente declarados: `identity.preferred_name`, `identity.self_reported_name`, `professional.works_at`, `professional.role`, relações e pedidos explícitos de lembrar. `SELF_REPORTED` tem alta qualidade de fonte; declaração sobre terceiros recebe `THIRD_PARTY_STATEMENT`.

Sono, comida, trânsito, calor e prazos imediatos ficam `ARCHIVE_ONLY`. Passwords, OTPs, 2FA, API keys, private/recovery keys e tokens ficam `BLOCK`, redigidos e fora do índice, mesmo com pedido explícito de lembrar.
