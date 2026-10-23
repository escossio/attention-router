# Investigador V0 — contrato de admissão de origem e escopo

**Estado:** PROPOSED · **Marco:** #339 · **Prova de código offline:** #353.

## Invariante de autoridade

Um alerta é dado **não confiável**. Identificadores, nomes de tenant, nomes de host, timestamps e campos de autorização que aparecem no evento não concedem autoridade. Um operador/adaptador autenticado é identificado antes da construção do `SourceEvent` autorizado. A fonte e o tenant vêm de um binding mantido pelo serviço, **nunca do payload**.

## Prova HMAC sintética do PR #353

O protótipo independente usa um registro local e sintético de `principal_id -> source, tenant_id, key`. O envelope inclui principal, horário, nonce, content type, ID de evento e bytes originais. Uma representação de comprimento prefixado de campos e SHA-256 do original é assinada com HMAC-SHA256; os campos vinculados à credencial também compõem o MAC. A verificação utiliza comparação de assinaturas resistente a diferenças triviais de tempo.

Depois de verificar formato, credencial, horário, assinatura e unicidade do nonce, o protótipo cria um `SourceEvent` com o tenant/source do registro confiável. Um campo `tenant_id` ou uma instrução textual dentro do corpo do evento não tem poder de alterá-los.

**Esta assinatura não é contrato de acesso real à API do Zabbix.** Qualquer integração com Zabbix, Linux, CI ou provedor precisa ser estudada conforme suas interfaces públicas e políticas de autenticação. Não presumir webhook, bearer token ou segredo de origem já provisionado.

## Gaps bloqueantes antes do uso real

1. Identidade confiável e credenciais provisionadas/rotacionadas fora do Git, com escopo por ambiente, conexão, tenant e recurso autorizado.
2. Replay ledger **durável e transacional** entre instâncias/restarts. O cache in-memory do protótipo não oferece isso e constitui falha explícita para produção.
3. Idempotência por evento real (source_id + event_id + conteúdo), sem transformar retries legítimos em incidentes duplicados e sem permitir conflito de bytes com mesma identidade.
4. Snapshot do seed armazenado antes do primeiro comando do investigador, com proveniência, conteúdo efetivamente recebido e hash verificável; recibo de persistência antes de ACK.
5. Limite por principal, rate limiting, volume de bytes, timestamps e timeout de origem. Sanitização de logs e respostas de erro, sem segredos ou payloads.
6. Isolamento de escopo do tenant também na consulta às ferramentas e nas views/bancos operacionais; HMAC em uma fronteira não autoriza atravessar todas as fronteiras.
7. Mecanismo de revogação, expiração, rotatividade de chave e auditoria de negações sem divulgar a chave.
8. Tratamento de falha de armazenamento/nonce: fail closed, sem admissão parcial ou sessão órfã.

## Critérios de aceitação offline

- Alterar bytes, nonce, horário, identificador do evento ou identidade quebra assinatura.
- Origem/tenant incompatível com o binding resulta em recusa; payloads não conseguem substituir a autoridade.
- Replay no mesmo processo é bloqueado; teste separado demonstra que processos distintos ainda aceitam o mesmo nonce sem ledger durável.
- Recusas retornam erro estável e opaco.
- Fixtures não contêm chaves ou dados reais. O código não ativa serviços nem muda sistemas existentes.

**Sem deploy, IP/VLAN novo, container, PostgreSQL operacional, execução de modelo real ou mudanças na MikroTik.**