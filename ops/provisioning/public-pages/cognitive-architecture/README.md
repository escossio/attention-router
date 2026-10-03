# cognitive.escossio.com

Fonte versionada da página pública do marco `MARCO-2026-10-03-ANDY-COGNITIVE-ARCHITECTURE`.

## Governança

GitHub é a fonte de verdade. O AGT não edita o conteúdo: ele faz checkout/fetch de um SHA existente no GitHub, valida esse SHA e executa `deploy.sh`.

A narração Zargan é um artefato derivado de `content.json`. No provider xAI, o identificador técnico atualmente exposto é `zagan`. O áudio não é fonte autoritativa; o manifest registra SHA-256 do conteúdo e do MP3.

## Publicação

1. aguardar CI verde e merge;
2. no AGT, atualizar o checkout canônico para o SHA de main;
3. executar `EXPECTED_SHA=<sha> ops/provisioning/public-pages/cognitive-architecture/deploy.sh`;
4. instalar o VirtualHost versionado;
5. adicionar o hostname `cognitive.escossio.com` ao Cloudflare Tunnel apontando para Apache em `127.0.0.1:80`;
6. validar HTTPS, conteúdo, áudio, karaokê e `GITHUB_SHA`.
