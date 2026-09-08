# Meta WhatsApp Setup

Consulta oficial feita em 2026-08-07:

- Meta WhatsApp webhooks overview: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview
- Meta messages webhook reference: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages
- Meta webhook endpoint creation/signature: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint/
- Meta Graph Webhooks getting started: https://developers.facebook.com/docs/graph-api/webhooks/getting-started/
- Official Meta Postman WhatsApp Cloud API collection: https://www.postman.com/meta/whatsapp-business-platform/collection/wlk6lh4/whatsapp-cloud-api

## Estado local

O hostname público desta instalação é:

`router.example.test`

Callback URL:

`https://router.example.test/api/v1/ingress/meta/whatsapp/webhook`

O código recebe webhooks Meta assinados no listener dedicado:

`GET|POST /api/v1/ingress/meta/whatsapp/webhook`

O control plane continua em `127.0.0.1:18100`. O ingress dedicado fica em `127.0.0.1:18101` e não possui `/api/v1/admin`, `/docs`, `/openapi.json` ou ingress sintético.

## Configuração necessária

Defina somente em `.env` local ou secret equivalente:

- `META_WHATSAPP_ENABLED=true`
- `META_VERIFY_TOKEN`
- `META_APP_SECRET`
- `META_APP_ID`
- `META_WABA_ID`
- `META_PHONE_NUMBER_ID`
- `META_GRAPH_API_VERSION=v26.0`
- `META_WEBHOOK_PUBLIC_HOSTNAME=router.example.test`
- opcional para diagnóstico read-only: `META_ACCESS_TOKEN`

Para consultar o verify token local sem colocá-lo em documentação:

```bash
python3 - <<'PY'
from pathlib import Path
for line in Path('.env').read_text().splitlines():
    if line.startswith('META_VERIFY_TOKEN='):
        print(line.split('=', 1)[1])
PY
```

## Meta Dashboard

No Meta App Dashboard:

1. Obtenha o App Secret em App settings.
2. Configure o callback URL HTTPS público: `https://router.example.test/api/v1/ingress/meta/whatsapp/webhook`.
3. Use o mesmo verify token configurado localmente.
4. Assine o campo `messages` da WhatsApp Business Account.
5. Confirme que a WABA está inscrita no app. A operação documentada pela coleção oficial para subscription é `POST https://graph.facebook.com/{Version}/{WABA-ID}/subscribed_apps`; faça isso somente com WABA/App corretos.
6. Envie uma mensagem de teste para o número WhatsApp Business e valide entrada inbound. Não há resposta automática.

## Segurança

O POST só é aceito com `X-Hub-Signature-256` válido, calculado com HMAC SHA-256 sobre os bytes exatos do body usando o App Secret. O listener rejeita assinatura ausente, malformada ou inválida antes de interpretar JSON como confiável.

O endpoint não envia mensagens, não baixa mídia e não chama Graph API durante processamento de webhook.

## Rollback Cloudflare

Para remover apenas o hostname público desta integração:

1. Remova a regra `hostname: router.example.test` de `/etc/cloudflared/config.yml`.
2. Valide com `cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate`.
3. Reinicie `cloudflared`.
4. Remova a rota DNS do tunnel para `router.example.test` pelo painel Cloudflare ou CLI autorizada.

Não remova o tunnel inteiro.

## Rotação

Para rotacionar secrets:

1. Gere novo `META_VERIFY_TOKEN` ou atualize `META_APP_SECRET` no `.env`.
2. Atualize o Meta Dashboard.
3. Reinicie somente os containers do projeto.
4. Rode `python -m scripts.meta_diagnostics`.
