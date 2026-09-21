# CHECKPOINT — ANDY OPS LIVE SUPERVISOR + GMAIL PRODUCT

Data: 2026-09-21

## Regra de retomada

Antes de qualquer implementação:

1. consultar a `main`, PRs e checks atuais no GitHub;
2. usar GitHub/runtime atual como autoridade;
3. não reconstruir trabalho já mergeado;
4. não tocar live, deploy, flags ou Gmail real sem autorização explícita;
5. manter `escossio/attention-router` como projeto principal;
6. manter o AGT como control plane e CI01/CI02/CI03 como workers de carga;
7. suítes PostgreSQL/full pesadas não devem cair silenciosamente no AGT;
8. o painel operacional é observabilidade out-of-band, não lógica de produto.

## Baseline GitHub

Main verificada antes deste checkpoint:

`1c8c9f90ef1038e7654b076252cc0ab8675a393a`

A PR #148 foi mergeada e tornou regra do repositório que validações pesadas usem o pool distribuído quando o orquestrador estiver disponível.

O runner Gmail governado está mergeado:
- PR #146: `de666ba5ecc54ce3c4189b86d32bc510d1153532`;
- PR #147 hardening: `0990cc19cd1eaa07daebf9533e72b285b4153dc6`.

## Andy Ops Live Supervisor V1

Foi criado um painel LAN-only e out-of-band para supervisão operacional.

Objetivo principal: responder rapidamente se as máquinas estão trabalhando e se o canal Chat -> Remote Desktop Commander -> AGT continua ativo quando a interface gráfica do ChatGPT parece travada.

### Compute

A tela mostra AGT, CI01, CI02 e CI03 com:

- CPU total em destaque;
- CPU por core em blocos inspirados no btop;
- temperatura quando hwmon expõe sensor confiável;
- estado RUNNING / IDLE / OFFLINE;
- trabalho CI atual;
- tempo decorrido;
- histórico recente de despachos distribuídos.

RAM foi deliberadamente excluída do V1 para reduzir ruído.

AGT é a autoridade de distribuição do pool. CI01/CI02/CI03 são executores.

### Chat / Console

A interface gráfica do ChatGPT não é tratada como fonte confiável de estado.

O painel observa o canal concreto:

`Chat -> Remote Desktop Commander -> AGT`

Descoberta importante: o Remote Desktop Commander instalado persiste um JSONL limitado de chamadas de ferramenta, incluindo timestamp, ferramenta, argumentos, saída e duração. O painel lê essa caixa-preta de forma read-only, resume argumentos e redige padrões óbvios de segredo antes de enviar dados ao navegador.

### Runtime proof do painel

O V1 foi validado com:

- serviço systemd ativo;
- health endpoint respondendo;
- polling de 1,5 s;
- CPU ao vivo nos quatro nós;
- temperatura real no AGT/CI01/CI02;
- CI03 exibindo N/A porque o guest KVM não expõe thermal telemetry;
- leitura dos summaries do distributed CI;
- leitura do histórico persistente do Remote Desktop Commander;
- renderização validada em Chrome headless.

O pacote público em `ops/provisioning/andy-ops-panel/` é sanitizado: endereços LAN e valores específicos do host ficam fora do Git e entram por environment file.

## Estado real do Gmail

O problema original de conexão do Gmail foi resolvido.

O fluxo real de assinante foi provado fisicamente:

`Android -> Conectar Gmail -> Google consent -> backend -> ProviderAuthorization criptografada -> channel.email CONNECTED`

O runner governado também foi provado ao vivo usando a autorização criada pelo aplicativo, sem token manual e sem provisioning paralelo.

Prova one-shot:

- selecionou 1 mensagem;
- aceitou 1 evento;
- 0 duplicatas;
- somente metadata;
- body_observed=false;
- attachments_observed=false;
- nenhum body, snippet, attachment bytes ou token persistido no evento.

O neutral ingress também foi despachado one-shot até a fronteira canônica:

`Gmail real -> Product Runner -> Neutral Ingress -> IntegrationInbox -> CanonicalEvent -> Timeline`

A única linha PENDING foi processada com:

- selected=1;
- processed=1;
- blocked=0;
- estado final PROCESSED;
- CanonicalEvent origin EXTERNAL_INBOUND;
- channel channel.email;
- Timeline MESSAGE_RECEIVED.

Isso não acionou Decision Engine automaticamente.

### Incremental history cursor

A próxima fatia, cursor durável baseado em Gmail users.history.list, está na PR #149.

PR: `feat: add durable Gmail history cursor`

Head atual verificado:

`c0db6d6191bde735a659ec29d58328f36625e3c2`

Estado no checkpoint:

- PR aberta;
- mergeable=true;
- todos os 8 checks concluídos com success;
- distributed-postgres: 438 testes PASS em 113 s;
- CI01: 102;
- CI02: 145;
- CI03: 191;
- CodeQL, python-tests, transport-tests, docker-build e secret-scan verdes.

A PR #149 ainda NÃO está mergeada e NÃO está deployada.

## Conclusão do Gmail

A integração fundamental está concluída e comprovada:

- conexão de assinante real;
- autorização persistida e criptografada;
- escopo exato gmail.metadata;
- runner server-side governado;
- refresh token usado somente em memória;
- leitura metadata-only da INBOX;
- neutral ingress;
- criação de CanonicalEvent/Timeline.

Ainda NÃO está concluído o comportamento contínuo/autônomo de produção.

Restam, em ordem lógica:

1. decisão humana de merge da PR #149;
2. deploy/migração do cursor somente com autorização explícita;
3. mecanismo de scheduling/automatic polling;
4. recuperação explícita para stale Gmail history cursor;
5. definição/implementação da ponte CanonicalEvent -> camada de atenção/decisão da Andy, sem reutilizar DTO legado;
6. validação live incremental após deploy autorizado.

Portanto: o “Gmail não conecta / não entra na Andy” foi resolvido. O Gmail contínuo, automático e dirigido à decisão ainda é frontier futuro.

## Regra de carga computacional

PR #148 tornou regra:

- AGT coordena;
- CI01/CI02/CI03 executam carga pesada;
- full PostgreSQL/migration-heavy/full suite não deve cair silenciosamente no AGT;
- fallback pesado local exige autorização explícita;
- o harness PostgreSQL falha fechado no control plane sem break-glass.

## Próxima retomada

No novo chat:

1. consultar primeiro a main atual e as PRs abertas;
2. verificar se #149 continua aberta ou já foi mergeada;
3. verificar a PR/branch do Andy Ops Live Supervisor;
4. não repetir canário Gmail manual já provado;
5. não reconstruir runner #146/#147;
6. não executar full PostgreSQL no AGT;
7. não fazer deploy live sem autorização explícita.

Para o painel, próximos incrementos devem ser guiados por uso real. V1 já entrega Compute + Chat/Console. Não transformar em “cockpit da NASA”.

Possíveis evoluções somente quando fizerem falta:

- integrar GitHub checks/PRs na mesma visão;
- registrar explicitamente o despacho no instante em que AGT envia o shard, em vez de depender apenas de processo/log;
- sinalizar STALE quando um job declarado ativo para de produzir evidência;
- melhorar a distinção entre interface do ChatGPT parada e executores ainda ativos.

## Fronteiras preservadas

- painel read-only;
- sem botões de execução;
- sem acesso ao banco de produção;
- sem segredos no Git;
- sem endereço LAN público;
- CI03 documentado apenas como KVM Virtualized Worker;
- nenhum deploy do Attention Router foi feito por este checkpoint.
