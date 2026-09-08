# Limites de Segurança

Este ciclo é privado e sintético. Não use números reais, mensagens reais ou dados pessoais.

Segredos ficam em `.env` com permissão restrita. `.env.example` não contém credenciais reais.
O banco não publica porta no host por padrão. A aplicação HTTP fica publicada explicitamente em
`127.0.0.1:18100` nesta fase.

Os adaptadores reais de Meta, WhatsApp, Home Assistant, Android, TTS e IA não existem nesta
fatia. A confiança termina nos mocks locais.

Políticas controlam divulgações permitidas. A política `Desconhecido` não revela localização,
cômodos, estado doméstico ou dispositivos.

Endpoints administrativos de política existem apenas para o simulador local. Autenticação e
autorização serão obrigatórias antes de qualquer exposição pública.

Na Fase 3, control plane usa bearer token administrativo configurado por `ADMIN_TOKEN`.
O token não é hardcoded, não é registrado no journal e é comparado com `hmac.compare_digest`.
Isso não substitui identidade/autorização de produção futura; é uma fronteira local explícita.

`/docs` e `/openapi.json` continuam acessíveis porque o bind é localhost. Eles não fazem parte
da API pública de webhook e não devem ser publicados numa VPS sem decisão explícita.
## Meta WhatsApp Boundary

O listener `attention-router-ingress` não registra rotas administrativas, `/docs`, `/openapi.json`, simulador ou synthetic ingress. Ele valida `X-Hub-Signature-256` antes de confiar no JSON do POST Meta.

Hostname público permitido para esta fronteira:

`https://router.example.test`

Dados pessoais possíveis no caminho Meta:

- `external_actor_id`: persistido em `actor_bindings` quando o operador cria binding; mascarado em listagens.
- `actor_id` externo: persistido dentro do payload normalizado de `inbound_events` para idempotência/auditoria operacional.
- conteúdo textual recebido: persiste como `inbound_text` da interação, pois é a entrada do motor.
- profile name: usado como nome de exibição quando não há binding, mas não é gravado em audit público.

Nunca registrar em logs/audit: `META_APP_SECRET`, `META_VERIFY_TOKEN`, access token, Authorization header ou assinatura completa.
