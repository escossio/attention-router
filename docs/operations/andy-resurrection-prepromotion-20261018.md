# Andy — homologação e fronteira de promoção

**18/10/2026 UTC. Homologação autorizada; nenhuma mutação produtiva autorizada.**
Complementa o [relatório causal](andy-resurrection-forensics-20261018.md) e
substitui o procedimento operacional v1 do [gate inicial](andy-resurrection-recovery-gate-20261018.md).
O GO/NO-GO final deve citar o SHA integral e os checks concluídos no ledger
da [PR #380](https://github.com/escossio/attention-router/pull/380)/
[issue #376](https://github.com/escossio/attention-router/issues/376).
Checks de outro SHA, coleta de testes ou simulação não autorizam promoção.

## Pacote definitivo e isolamento

O [manifesto v2](../../config/examples/andy_visitor_recovery.proposed.json)
cria um blueprint **novo** em draft/OBSERVE e propõe uma segunda versão imutável
published/AUTO_ALLOWED. Não promove o blueprint pessoal existente nem reutiliza
o canário food_service. O tenant é exatamente o admitido pelo Transport,
verificado privadamente contra a entrada de teste e a policy efetiva.

Hashes canônicos do pacote revisado: manifesto
`d5b5691115114fb48ca93407361bdeef920fe4a095d7bf3443210e3d6cfe817a`;
criação `cc6f5369bdad6108cdef7e1ad35f77486a5d2ba51350c1d9304fe941468aa5a0`;
ativação `43357206ff32705dc7d823af08f2e41e5528b25e2196871ad7ef8d80cad55076`;
policy `444775fee3bced1e7c4a4c48defb655ea81b7abe6f01022ecbcb2f7dfa296c44`.
O preflight puro não atesta instalação real do guard nem autentica o tenant;
essas provas vêm do runtime e do artefato de imagem, separadamente.

O candidato contém `behavior.visitor_default_contract=text-only-v1` e requer
`AGENT_DECISION_DEFAULT_VISITOR_ONLY=true`. O marcador persiste na versão e
aplica o contrato mesmo se a flag desaparecer ou um binding referenciar seu ID.
O default é permitido apenas em mensagem direta externa elegível, audiência
unknown e **sem binding ativo**. Não resolve identidade por PN/LID, não muda
actor category e não converte posse de telefone em OWNER_COMMAND.

O runtime valida domínio general, publicação, versão pertencente ao blueprint,
imutabilidade/checksum, audiência/canal e ações textuais. Policy deve declarar
explicitamente a autoridade conversacional, com allowed_actions textuais e
capabilities/disclosures vazios. OBSERVE/silêncio continua restritivo.
O mesmo contrato é revalidado no caminho existente de execução antes do envio.
Para intents do contrato restrito, essa revalidação relê blueprint, interação e
políticas/versões do tenant após flush, evitando autoridade antiga em uma sessão
ORM que sobreviva a uma alteração administrativa concorrente.
O candidato é excluído do ranking não direto.
Se a flag visitor-only estiver ativa sem um default não vazio, settings recusa
o startup com `VISITOR_DEFAULT_BLUEPRINT_REQUIRED`, tornando a perda parcial
de configuração uma falha explícita.

Contexto do visitante conserva mensagem e histórico **comprovadamente público**
do mesmo contato/tenant, com evidência anterior `visitor_default.context_built`
do contrato text-only-v1. Histórico legado sem essa prova fica fora do prompt;
isso impede que a desativação de um antigo binding owner exponha conversas
privadas ao visitante atual. A seleção continua limitada aos seis registros
anteriores elegíveis e exige prova de entrega para as respostas do assistente.
O guard retira sujeito representado, estado privado, facts, decisions, diretivas,
inventário de ferramentas e autoridade da plataforma. Propostas hostis de
capacidade continuam passando pela negação independente de policy/owner/grant.
Nenhuma ferramenta, Gmail, comando administrativo ou representação de owner é
concedida. Outros blueprints explícitos sem o marcador mantêm seu contrato.
Binding ativo sem blueprint próprio não recebe o novo default: essa limitação
é intencional para evitar herança de autoridade nesta recuperação.

A policy unknown muda em nova versão, com snapshot/CAS; seu alcance continua
mais amplo que o visitante. O guard e os gates de entrega impedem que esse
delta dê autoridade automática ao candidato em grupos/non-direct. O motor de
grupos independente e os respectivos controles permanecem como no baseline.

| Arquivo alterado nesta fase | Finalidade |
| --- | --- |
| `attention_router/config.py` | Flag opt-in e rejeição de default ausente quando a flag está ativa. |
| `attention_router/application/decision_pipeline.py` | Guard de seleção/contexto e histórico público comprovado. |
| `attention_router/application/execution.py` | Revalidação do guard e atualização das leituras de autoridade antes do envio. |
| `attention_router/infrastructure/repository.py` | Tenant correto na auditoria da nova versão de policy. |
| `config/examples/andy_visitor_recovery.proposed.json` | Spec/policy/contrato v2 exatos para revisão. |
| `scripts/forensics/andy_visitor_recovery_admin.py` | CLI administrativa com snapshot, CAS, rollback e reconciliação. |
| `scripts/forensics/andy_worker_composition_snapshot.py` | Verificação de composição e modelos privados inertes. |

Preflight, harness e regressões também foram atualizados. As configurações
produtivas continuam sem edição; os overlays propostos existem somente no
pacote privado de revisão.

## Sete provas para o GO/NO-GO

| Gate | Prova reproduzível exigida |
| --- | --- |
| 1. Visitante sem binding | Criação e ativação administrativas em PostgreSQL descartável → seleção → Agent sintético → autonomia → intent/outbox únicos → adapter sintético DONE. Nova mensagem após rollback bloqueada. |
| 2. Autoridades separadas | Matriz original, contexto privado envenenado, grant privilegiado preexistente, pedidos administrativos/Gmail e bindings owner negados; nenhuma chamada ao executor de owner. |
| 3. Grupos/non-direct | Guard rejeita seleção do candidato, inclusive ranking/referência explícita; regressões do motor de grupos preservadas e flag perdida continua segura pelo marcador. |
| 4. Tenant | Evidência atual READ ONLY compara Transport/entrada/policy; versões e requests divergentes rejeitados; auditoria de policy registra tenant correto. |
| 5. Administração/rollback | CLI local e biblioteca transacional ensaiadas em PostgreSQL descartável, snapshots privados, aprovação por fase, CAS, falha atômica, replay e edição concorrente protegidos. |
| 6. Worker completo | Snapshot privado de cinco Compose files + um env, render canônico equivalente à label viva e comparação de gates/redes/comando/healthcheck; promoção e rollback preservam a pilha integral. |
| 7. SHA exato | Python, PostgreSQL, Docker, Transport, architecture guard, secret scan e CodeQL exigidos passam no HEAD definitivo; sem herdar certificação 478f644. |

Os adapters de Agent/Transport são sintéticos e não usam rede externa. DONE de
fixture não é ACK do WhatsApp, nem prova de saúde/quota do provedor real.
A aceitação real exige canários novos em janela aprovada, após o GO técnico.

## Caminho administrativo reproduzível

O Builder conversacional continua criando observe e rejeitando promoção por
entrevista. O incremento é uma ferramenta **local do operador**, sem endpoint,
grant remoto ou bypass do motor de autorização:
[andy_visitor_recovery_admin.py](../../scripts/forensics/andy_visitor_recovery_admin.py).
Usa modelos, schema, versionamento e auditoria existentes. Corrige também o
tenant do evento `policy_version_created`, antes omitido pelo helper existente.

`prepare` é REPEATABLE READ/READ ONLY/timeout3s. Lê tenant ativo, policy/versão/checksum atuais,
captura todos os campos da policy e fixa IDs novos ainda inexistentes e hashes
do pacote. `create-observe`, `activate` e `rollback` exigem arquivo privado de
aprovação da **operação e snapshot exatos**, mais referências do operador e da
aprovação real. Arquivo não autentica humano remoto: a autoridade vem da sessão
administrativa do host e da aprovação operacional prévia do usuário.

Comandos da CLI recebem somente caminhos privados; a conexão é fornecida por
`ANDY_RECOVERY_DATABASE_URL`, sem fallback para uma conexão implícita. Não passar
credenciais, UUIDs produtivos ou texto de clientes em argv/transcript.

```bash
python -m scripts.forensics.andy_visitor_recovery_admin prepare \
  --manifest config/examples/andy_visitor_recovery.proposed.json \
  --request /private/request.json --output /private/plan.json
python -m scripts.forensics.andy_visitor_recovery_admin create-observe \
  --snapshot /private/plan.json --approval /private/create-approval.json \
  --output /private/observe.json
python -m scripts.forensics.andy_visitor_recovery_admin activate \
  --snapshot /private/observe.json --approval /private/activate-approval.json \
  --output /private/active.json
python -m scripts.forensics.andy_visitor_recovery_admin rollback \
  --snapshot /private/active.json --approval /private/rollback-approval.json \
  --output /private/rolled-back.json
python -m scripts.forensics.andy_visitor_recovery_admin reconcile \
  --snapshot /private/active.json.pending --output /private/reconciled.json
```

Esses caminhos são exemplos; o pacote privado contém os valores reais. **Os três
comandos mutadores não foram executados contra produção.** Approval sintético
de teste não pode ser reutilizado. Cada fase usa locks Tenant → Policy → Blueprint,
CAS e transação; versões históricas não são editadas. O retorno do default é
um overlay proposto, não uma mudança em settings/env nem um restart.

Banco e arquivo não formam uma transação única. Antes do commit, a CLI grava
e sincroniza um journal privado imutável `.pending`, com recibo completo proposto
e resultado UNKNOWN. Se o commit ocorrer mas a finalização do recibo falhar,
`reconcile` comprova o estado exato das duas versões/policy e da auditoria da
operação em READ ONLY, e produz novo recibo. Estado misto, edição concorrente
ou ausência dessa prova permanecem NO-GO/UNKNOWN: não repetir o mutador nem
compensar automaticamente. O ensaio injeta falha de gravação após commit.

## Proveniência da imagem e composição completa

O Worker vivo é 4d06e4d, com código de grupos/0058 da PR #333 ainda não merged.
A imagem da antiga #380/main não continha esse código. O candidato preserva
**exatamente o baseline implantado**, sem incorporar o HEAD posterior dessa PR,
e acrescenta os controles revisados. A atualização da imagem é um delta
explícito da promoção futura; configurar o default na imagem antiga não instala
o guard. Nenhum rollout de Transport/API/Browser está incluído.

O banco observado está em `0059_maestro_relay_receipts`, incluindo grupos e
Relay. O procedimento **não executa alembic upgrade/stamp/downgrade** em produção.
Worker não deve substituir a administração do schema ou os serviços MAESTRO.

Snapshot privado guarda inspect/imagem, cinco overlays na ordem original, env,
hashes/permissões e render completo. Paridade foi comparada campo a campo e
pelo hash Compose do render normalizado. O hash direto dos arquivos difere por
representação/resolução; o hash do roundtrip normalizado coincide com a label
do contêiner vivo. Não confundir defaults da imagem com as variáveis declaradas.

O helper de composição produz manifestos privados de implantação/reversão e
valida que só imagem, default, flag e os dois metadados de SHA mudem.
`ATTENTION_ROUTER_BUILD_SHA` e `RUNTIME_HEAD` são explicitamente declarados na
pilha viva e divergem dos defaults da imagem; devem apontar ambos ao novo SHA
certificado. Trocar somente a imagem manteria metadados sobrepostos antigos.
Rollback restaura os valores originais exatos, sem expô-los publicamente.
A pilha original fica inteira, com
um overlay adicional estreito. Ordem, project, service, comando, entrypoint,
redes, mounts, healthcheck e demais gates são preservados. Não usar Compose
mínimo, `down`, `--remove-orphans`, build local ou recriação de dependências.
Rollback fixa a imagem viva por image ID local; RepoDigests vazio não prova um
digest de registry. A imagem candidata deve ter SHA OCI/ID e artefato de build
associados à certificação definitiva antes do GO.

## Procedimento na fronteira de produção

1. Obter autorização final explícita para criar/publicar o novo candidato,
   aumentar sua autonomia textual, versionar a policy, configurar default/guard,
   atualizar somente imagem e metadados de SHA do Worker e recriá-lo na janela controlada.
2. Renovar snapshots privados de composição e linhas administrativas. Revalidar
   tenant/versão/checksums e o baseline de imagem/gates; drift significa NO-GO,
   não autorização para corrigir outros serviços.
3. Renderizar a pilha completa com overlay estreito e comparar com o manifesto.
   Confirmar artefato certificado e rollback disponíveis antes da parada.
   Na janela aprovada, parar **somente Worker** pela composição integral,
   antes de publicar o candidato. O Worker antigo não conhece o marcador e
   poderia selecionar o candidato publicado por ranking non-direct durante
   uma atualização sem essa barreira.
4. Executar o caminho administrativo aprovado por fases, conservar recibos
   privados e iniciar/recriar somente Worker com imagem e composição aprovadas,
   `--no-deps --no-build`, sem migração. Conferir settings/proveniência carregados.
   Não alterar bindings, tenants, identidades, owner controls, grants, Gmail,
   cursores, Relay, MAESTRO ou observabilidade. Ingress/Transport permanecem
   ativos; duração da janela e freshness são verificadas sem replay histórico.
5. Somente na janela autorizada, usar duas mensagens novas e identidades de
   teste consentidas; comprovar seleção, autoridade, intent, outbox, despacho e
   evidência de entrega. Não reenviar backlog nem tratar trace próximo como
   correlação causal. Tracing ausente continua NOT_PROVEN, sem bloquear função.
6. Registrar resultado real em STATUS/issue/checkpoint. Interromper aceitação
   se houver domínio/tenant incorreto, autoridade privada, duplicata, falha
   funcional ou discrepância de composição.

## Rollback e limites

Parar somente Worker e aplicar primeiro rollback administrativo com CAS para
restaurar a policy anterior e retornar o candidato a draft/OBSERVE, preservando
ambas as versões e auditoria. Só então restaurar a pilha/config e imagem
originais do Worker, retirando o default novo. O código antigo não deve voltar
a rodar enquanto o novo candidato continuar publicado e selecionável por ranking.
Edição concorrente bloqueia a reversão automática: reconciliar, sem sobrescrever
mudanças de terceiros. Snapshot direcionado e histórico imutável permitem esse
rollback sem restaurar indiscriminadamente todo o banco.

Não apagar intents, outboxes, entregas ou histórico. Intents/outbox pendentes
continuam sob os gates existentes; resultado ambíguo não autoriza replay.
Mensagem já enviada não é reversível. Validar a retomada do estado anterior e
que nenhum serviço vizinho mudou. A prova descartável certifica transações e
procedimento; o tempo de parada/restart real só será medido na janela aprovada.

**Continuidade ao vivo:** recupera admissões novas legítimas com os controles
descritos. **Aceleração histórica:** deliberadamente adiada; replay de mensagens
antigas pode enviar respostas fora de contexto e não faz parte desta promoção.
Não há novo serviço, rede, porta, IP, capacidade ou integração.
