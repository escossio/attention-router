from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen

from attention_router.config import settings
from attention_router.web.meta_security import sign_meta_body, verify_meta_signature


def configured(value: str | None) -> str:
    return "configured" if value else "missing"


def local_get(url: str) -> str:
    try:
        request = Request(url, headers={"User-Agent": "attention-router-diagnostics/1.0"})
        with urlopen(request, timeout=5) as response:
            return f"reachable:{response.status}"
    except URLError as exc:
        return f"unreachable:{exc.reason}"


def main() -> None:
    body = json.dumps({"synthetic": True}, separators=(",", ":"), sort_keys=True).encode()
    signature = sign_meta_body(body, settings.meta_app_secret or "synthetic")
    print(f"META_WHATSAPP_ENABLED={settings.meta_whatsapp_enabled}")
    print(f"META_VERIFY_TOKEN={configured(settings.meta_verify_token)}")
    print(f"META_APP_SECRET={configured(settings.meta_app_secret)}")
    print(f"META_APP_ID={configured(settings.meta_app_id)}")
    print(f"META_WABA_ID={configured(settings.meta_waba_id)}")
    print(f"META_PHONE_NUMBER_ID={configured(settings.meta_phone_number_id)}")
    print(f"META_GRAPH_API_VERSION={settings.meta_graph_api_version}")
    print(f"META_WEBHOOK_PUBLIC_HOSTNAME={configured(settings.meta_webhook_public_hostname)}")
    print(f"META_ACCESS_TOKEN={configured(settings.meta_access_token)}")
    local_status = local_get(f"http://127.0.0.1:{settings.ingress_http_port}/health/live")
    compose_status = local_get(f"http://ingress:{settings.ingress_http_port}/health/live")
    print(f"INGRESS_LOCALHOST={local_status}")
    print(f"INGRESS_COMPOSE_SERVICE={compose_status}")
    if settings.meta_webhook_public_hostname:
        print(f"INGRESS_PUBLIC={local_get(f'https://{settings.meta_webhook_public_hostname}/health/live')}")
    else:
        print("INGRESS_PUBLIC=skipped:missing_public_hostname")
    print(f"SIGNATURE_VALIDATION_LOCAL={verify_meta_signature(body, signature, settings.meta_app_secret or 'synthetic')}")
    if settings.meta_waba_id and settings.meta_access_token:
        url = f"https://graph.facebook.com/{settings.meta_graph_api_version}/{settings.meta_waba_id}?fields=id,name"
        request = Request(url, headers={"Authorization": "Bearer " + settings.meta_access_token})
        try:
            with urlopen(request, timeout=5) as response:
                print(f"GRAPH_READONLY_WABA=reachable:{response.status}")
        except URLError as exc:
            print(f"GRAPH_READONLY_WABA=unreachable:{exc.reason}")
    else:
        print("GRAPH_READONLY_WABA=skipped:missing_waba_id_or_access_token")


if __name__ == "__main__":
    main()
