# MAESTRO — compatibilidade de contrato com Occurrence Relay V0

**Estado:** validador puro em branch isolada; sem HTTP, banco, migração, contêiner, integração, deploy ou merge.

## Reuso encontrado

O Attention Router já inclui web/integration_ingress.py, integrations/admission.py, integrations/tenant_binding.py, contracts/integration.py e IntegrationInboxRow persistido no PostgreSQL. Esta infraestrutura fornece admissão com binding, autenticação, recibo após commit e idempotência para o contrato inbound_event. Não é, porém, MAESTRO.

O relay envia contrato occurrence-relay-v0-draft e cabeçalho Idempotency-Key. A rota genérica recusa explicitamente esse cabeçalho e espera um JSON de formato diferente. Não reutilizar essa rota como receptor MAESTRO sem estabelecer uma adaptação segura e um contrato de persistência próprio ao papel.

## Entrega desta etapa

- Módulo attention_router/maestro/relay_contract.py faz validação tipada, sem efeitos colaterais, do envelope atual do Relay V0.
- A identidade de tenant, origem e instância é fornecida exclusivamente por TrustedRelayBinding, que precisa resultar de autenticação externa. O conteúdo JSON não concede escopo.
- Identidade de recibo futuro: tupla (tenant, source, instance_id, occurrence_number), evitando confundir números locais entre diferentes instalações do relay.
- Confere versão, cabeçalho Idempotency-Key, tipagem, limites de payload, ausência de campos duplicados ou desconhecidos, timestamps com timezone, recuperação vinculada ao problema e digest canônico.
- Nunca chama LLM, monitor, Investigador, executor ou motor de autorização; não ordena filas, não define prioridade, não transforma normalização em ação.
- Não emite HTTP 2xx. O resultado do validador não é recibo durável.

## Pendência do contrato real de admissão

O receptor MAESTRO deve aceitar evento somente após identificar credencial de serviço autenticada e realizar commit atômico no PostgreSQL de (tenant, source, instance_id, occurrence_number, digest, metadados). Replay do mesmo ID e mesmo digest devolve o mesmo recibo; mesma identidade com digest diferente falha; persistência indisponível não pode devolver 2xx. Recuperação precisa ser evento novo com vínculo ao anterior, não fechamento automático.

A próxima decisão técnica é escolher como aproveitar a infraestrutura já existente de admissão sem violar seu contrato ou introduzir outra fonte de verdade. Este módulo NÃO instala uma API e não supõe que MAESTRO esteja operacional.

## Rede / autoridade / rollback

Nenhum fluxo ou segmento de rede alterado nesta etapa. Futuro: Relay → receptor MAESTRO por HTTPS autenticado, e receptor → persistência já autorizada. A autoridade decorre de identidade de serviço e binding, nunca apenas de reachability. L2 dedicada: não. IP dedicado: não. IP estático: não. Rede adicional: não definida/necessária no incremento. Rollback: descartar branch, pois nada foi implantado.

Live continuity: eventos novos do Zabbix intocados, integração real desativada. Historical acceleration: importação histórica não realizada, por não haver contrato/cursor aprovado.

## Testes

11 testes offline em tests/test_maestro_relay_contract_v0.py, sem rede, segredos operacionais nem PostgreSQL. Cobrem entradas válidas, problema/recuperação, tenant, binding de instância, chave de idempotência, replay canônico, chaves duplicadas, tipos, limites e timestamps. Testes não comprovam aceite durável no MAESTRO real.

**Próximo gate:** certificação CI do SHA, revisão do encaixe na infraestrutura de admissão e prova PostgreSQL no worker antes de considerar um receptor executável. PR em draft; sem merge automático.
