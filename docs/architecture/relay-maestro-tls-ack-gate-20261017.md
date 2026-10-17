# Relay → MAESTRO: fronteiras TLS, recibo verificável e gates

**Data:** 2026-10-17
**Estado:** CANDIDATO LOCAL PARA REVISÃO — não implantado, sem autorização de merge/deploy
**Escopo:** PRs #367 (Relay V0) e #368 (receptor MAESTRO), AGT01, Apache já existente, redes já existentes.

## Evidências da inspeção (não são homologação)

- Relay de estágio `andy-occurrence-relay-stage`: imagem `696b19b`, Docker `bridge`, IP privado observado em inventário operacional não publicado, ingress publicado apenas em `127.0.0.1:18123`. `MAESTRO_URL` ausente.
- O Relay abre TCP para o gateway local Docker `<gateway-docker>:443`. Apache 2.4.68 escuta em 443, com módulos SSL, proxy_http e rewrite. O virtual host HTTPS atualmente detectado é `default-ssl.conf`.
- Certificado atual: autoassinado para `agt01`, SAN `DNS:agt01`. O contêiner Relay rejeitou a validação usando suas CAs padrão. A prova TCP **não** aprova autenticidade TLS.
- O ingresso de aplicação (não confundir com ingresso interno) é `<ingresso-estagio>:<porta-http>`, HTTP/Uvicorn, na `andy-vlan212-ingress-stage`. Teste TCP AGT→ingresso: PASS. A rota do AGT para essa rede passa por `<gateway-da-rede-de-controle>` via interface de controle existente; não é loopback nem conexão inteiramente local ao kernel.
- O ingresso atual NÃO implementa a rota nova do PR #368. A integração em produção ainda não possui certificado, binding/escopo, migração PostgreSQL ou ACK real homologados.

## Resolução local do problema de ACK (código candidato, não instalado)

O Relay deixou de considerar qualquer HTTP 2xx como entrega. Aceita somente:
- HTTP **202** + Content-Type JSON + corpo `{status:"ACCEPTED",receipt_id:<string>,occurrence_number:<inteiro igual ao enviado>}`; ou
- HTTP **200** + mesmo contrato, com `status:"DUPLICATE"`.

Recusa corpo vazio, HTML, 204, campos ausentes/extras, JSON duplicado, recibo em branco/fora do limite, status ou número divergentes, tipos incorretos e respostas >4096 bytes. Falha não remove item da outbox; permite retry idempotente. Continua bloqueando redirect.

O cliente ignora proxies herdados do ambiente: esta conexão privada precisa alcançar o endpoint configurado diretamente, sem `HTTPS_PROXY`. O cliente HTTPS usa `ssl.create_default_context()` com hostname checking e `CERT_REQUIRED`, suporta `MAESTRO_CA_FILE` quando houver CA privada aprovada. Não fornece opção de `verify=False` ou de suprimir hostname checking; CA inválida impede inicialização da entrega. `MAESTRO_URL` permanece ausente no estágio.

Teste local de TLS em loopback com certificado de vida curta confirmou recusa sem confiança, recusa de hostname divergente e aceitação com CA e hostname corretos, sem falar com serviços AGT.

## Arquitetura candidata — dois limites distintos

1. **Relay → Apache (TLS 1):** nome DNS privado aprovado, certificado com SAN correspondente, CA confiável carregada no Relay por montagem de arquivo somente-leitura. Preferir virtual host/porta restritos ao endereço do gateway Docker, sem expor a rota publicamente. Avaliar cuidadosamente conflito com a porta 443 e vhosts atuais antes de escolher o listener. Não pressupor IP fixo do contêiner, DNS existente ou autorização pela simples origem IP.
2. **Apache → ingresso MAESTRO (TLS 2):** o backend atual recebe HTTP sobre caminho roteado por MikroTik. É inadequado declarar confidencialidade ponta a ponta de Bearer sem proteção adicional. Necessário HTTPS interno com identidade verificada no destino (`SSLProxyVerify require`, CA aprovada e checagem de hostname no Apache), ou avaliação formal documentada que aceite explicitamente o risco residual do trecho HTTP. **Política recomendada: NO-GO com Bearer real enquanto o trecho interno estiver em HTTP.**
3. **Ingresso → PostgreSQL existente:** somente após aplicação homologada da migration versionada, credencial/binding de serviço, escopo `maestro:relay:write` e semântica 202/200 após commit; sem novo PostgreSQL, agente ou intermediário.

**O que NÃO fazer:** não substituir o certificado autoassinado atual de outros serviços; não instalar CA do servidor no trust store geral da máquina por conveniência; não desligar verificação TLS; não colocar o Relay em macvlan, criar VLAN, IP extra ou liberar internet; não ligar o webhook Zabbix.

## Pré-requisitos técnicos para backend TLS

O processo atual Uvicorn oferece **HTTP em 18101**. Alterar essa porta para HTTPS diretamente pode quebrar clientes existentes. Inventariar todos os consumidores e verificar se há forma de ativar um listener TLS restrito **no mesmo serviço já existente** sem alterar o contrato/fluxo HTTP ativo. Se exigir outro processo ou contêiner, tratar como alternativa arquitetural que demanda revisão, não assumir implantação automática.

A alternativa de TLS direto Relay→ingresso pode dispensar o Apache, mas exige endereço/certificado/porta TLS reais no ingresso e liberação mínima de rota; comparar com dois hops e com o Apache existente antes de aprovar. Escolher explicitamente fronteira de confiança; não esconder HTTP com nomenclatura HTTPS.

### Decisão condicionada sobre o transporte TLS

Comparação de soluções sob a premissa de preservar o ingresso HTTP existente:

| Alternativa | Efeito no ingresso | Segurança da credencial | Estado |
| --- | --- | --- | --- |
| Relay → ingresso HTTPS direto em nova porta | Necessita listener TLS adicional, mantendo HTTP legado | Pode evitar salto interno em claro | Preferível **se** a operação de segundo listener for aprovada e provada |
| Relay → Apache TLS → ingresso HTTPS | Necessita TLS adicional no backend e configuração proxy | Dois trechos cifrados, duas identidades verificadas | Maior complexidade; manter como fallback |
| Relay → Apache TLS → ingresso HTTP | Nenhum listener novo no ingress | Bearer em claro no salto roteado | **NO-GO** para segredo real pela política desta revisão |
| Trocar a porta HTTP atual por HTTPS | Muda contrato para consumidores existentes | Exige migração simultânea dos consumidores | **REJEITADA** sem inventário e plano de migração |

Uvicorn instalado hoje oferece HTTP em um único listener observado. TLS adicional sem
interromper clientes HTTP exige recurso/processo de escuta adicional ou configuração
equivalente comprovada; não declarar uma solução "sem novo listener" por presunção.

**Gates de decisão:** inventariar consumidores HTTP, demonstrar configuração TLS candidata
em CI com o mesmo app e PostgreSQL descartável, comprovar DNS/SAN/CA e identidade do
cliente, demonstrar ausência de rotas públicas, testar rollback e preservar o ingresso
legado. Não alterar a porta 18101 existente nesta fase.

**Certificações disponíveis:** PR #368 SHA `2c8bfeec22aefc54ff1fbfa068cadbda91e109d5`
passou no worker CI03 a suíte PostgreSQL com 470 testes e a integração HTTPS do receptor
por cliente sintético em banco descartável. No workspace do Relay, 30 testes focados
passaram, incluindo socket HTTPS real para perda de ACK e replay. As duas pontas ainda
**não foram certificadas juntas** no mesmo teste com o PostgreSQL descartável; antes
desse teste é necessário preparar candidato integrado de SHA exato para o CI distribuído.

### Requisitos de virtual host Apache, se esta alternativa vencer

- Listener privado (IP/porta escolhidos após inventário), SNI/ServerName verificados, certificado específico aprovado; preservar vhosts públicos e MGMT.
- Permitir somente `POST /api/v1/ingress/maestro/occurrences`, sem query string; rejeitar outros métodos/paths, redirects, e comportamento de fallback que retorne 2xx.
- Não expor proxy aberto, não aceitar backend dinâmico via cabeçalhos e não confiar em cabeçalhos Forwarded fornecidos pelo cliente; revisar stripping/preservação de `Authorization` e `Idempotency-Key` sem logar segredos.
- Limitar tamanho, cabeçalhos e timeouts ao contrato; não registrar body, bearer, payload ou identificadores privados.
- Se usar proxy TLS ao backend, verificar certificado, cadeia, nome e erro de handshake sem fallback para HTTP. Não usar `SSLProxyVerify none` ou `SSLProxyCheckPeerName off`.
- Filtrar tráfego de ingresso não previsto no host e firewall com regras mínimas, após revisão de regras pré-existentes e sem abrir tráfego público.

## Homologação antes do GO

1. Congelar SHAs de PRs e inventariar consumidores, certs/CA, DNS, Apache, flags e versões ativas.
2. Certificar testes Python focados (ACK estrito, retry após ACK perdido, TLS loopback e casos negativos) e gates distribuídos CI no SHA exato.
3. Validar com inspeção separada o Apache SNI/ACL/route e o TLS upstream: falha de CA e SAN do cliente e do proxy precisa bloquear, não fazer bypass.
4. Testar fluxo sintético com o binário real do Relay + app receptor + PostgreSQL descartável e evento fictício; exigir 202 ACK durável, replay 200 com o mesmo `receipt_id`, conflito 409 e erro 401; não usar a DB operacional.
5. Testar reinicialização, erro 503, perda de ACK, reinício de processo, rollback e ausência de alterações nas VLANs/serviços em operação.
6. Aprovar separadamente plano de deploy, backup/restore, migração, identidade de serviço, credenciais e rollback. **Sem autorização de Zabbix real** nesta etapa.

## Resultado da presente fase

**ACK:** correção e testes locais candidatos.
**TLS do cliente:** suporte e teste de confiança candidatos.
**Rede:** TCP comprovado; nenhuma mudança MikroTik necessária para transporte básico.
**TLS do ingresso e criptografia do segundo trecho:** PENDENTE; não há prova de endpoint seguro em ingresso de estágio.
**Estado:** NO-GO de implantação/credenciais reais; PRs draft, ambiente live intocado.
