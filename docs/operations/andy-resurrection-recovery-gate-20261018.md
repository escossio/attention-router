# Andy — candidato e gate de recuperação do atendimento geral

**18/10/2026. Estado: procedimento para revisão, sem autorização operacional.**
Relaciona-se ao [relatório causal](andy-resurrection-forensics-20261018.md).
Não é um script de aplicação, um novo serviço ou uma promoção de blueprint.

## Resultado pretendido e menor delta

Uma mensagem direta externa legítima de um visitante pode produzir uma resposta
conversacional por `respond` ou `request_information`, mesmo sem binding.
Continuam obrigatórios tenant correto, origem externa, identidade parcial segura,
canal de retorno validado, policy, readiness, freshness, owner controls e last mile.

O mecanismo existente já faz isso. O delta operacional proposto é um **blueprint
dedicado de atendimento geral**, em versão explícita do tenant atendido pelo
Transport, e `AGENT_DECISION_DEFAULT_BLUEPRINT_ID=<id aprovado>` no manifesto
efetivo do Worker. A variável é única no processo, porém `_blueprint` exige
igualdade de tenant; não habilita atendimento em outro tenant.

O default existente também é consultado em non-direct/grupos e em bindings sem
ID explícito, inclusive owner. Pode mudar a seleção/contexto determinístico
nesses casos, mesmo sem conceder envio. O preflight e os testes devem registrar
esse alcance; apenas bindings com blueprint explícito mantêm sua precedência.
Não se apresenta a variável como configuração exclusiva de visitantes.

A policy proposta também explicita `allowed_actions` com as duas ações textuais.
A policy selecionada no teste real tem legacy soft_ping, zero capabilities e
zero disclosures; não possui conversational_execution explícito. O pacote torna
esse contrato textual explícito em nova versão, se aprovada. A mudança pode
afetar outros fluxos que selecionem a mesma policy unknown; esse alcance exige
revisão, sem afirmar que os demais fluxos ficam semanticamente idênticos.

Não é necessário mudar o lookup de actor, inserir binding para o telefone de
teste, habilitar legacy fallback, reiniciar todos os serviços ou trocar imagem.
Não se reutiliza o canário food_service. O blueprint pessoal existente permanece
draft/observe; não se edita nenhuma versão imutável existente.

## Contrato concreto do candidato para aprovação

| Campo/controle | Valor ou restrição proposta |
| --- | --- |
| Finalidade | Responder perguntas gerais e pedir esclarecimento no mesmo chat externo. |
| Domínio | `general`; não food_service, fixture, canary ou administração pessoal. |
| Identidade | Andy, assistente conversacional; não se passa pelo proprietário. |
| Tenant | Exatamente o tenant admitido pelo envelope WhatsApp, resolvido privadamente. |
| Audiência | Visitante externo desconhecido e contatos comuns; não OWNER por default. |
| Ações textuais | `respond`, `request_information`. |
| Capacidades executáveis | Nenhuma concedida pelo candidato; preservar todos os gates separados. |
| Disclosures privados | Nenhum concedido ao visitante. Policy e standing directives devem comprovar negação. |
| Gmail/Google | Nenhuma leitura, consulta ou ação por conta da conversa pública. |
| Administração/infra | Nenhuma execução, aprovação implícita, representação de owner ou encaminhamento privilegiado. |
| Autonomia inicial de criação | OBSERVE, conforme o contrato atual do Builder. |
| Autonomia proposta após aprovação | Nova versão com `autonomy.execution_mode=AUTO_ALLOWED`, limitada pela policy a ações textuais. |
| Publicação | Somente a versão explicitamente aprovada após testes; nenhum publish silencioso. |
| Histórico/memória | Preservar isolamento por tenant/contato e disclosure; não importar, reseed ou reprocessar backlog. |

`execution_mode` tem precedência sobre `level` em `autonomy.blueprint_mode`.
Portanto adicionar AUTO_ALLOWED é **aumento explícito de autonomia**, mesmo se
um campo legado `level` ainda disser observe. O approval deve citar esse delta;
não apresentá-lo como mera correção cosmética de configuração.

O Builder atual cria blueprints em observe e rejeita patches conversacionais
de autonomia/status. Não há neste relatório uma afirmação de que exista um
endpoint público de promoção. A ativação deve seguir o procedimento administrativo
vigente ou uma ferramenta administrativa mínima revisada separadamente, com
autorização expressa; não contornar o Builder pela entrevista nem gravar SQL
ad hoc para experimentar em produção.

## Pacote preparado e prova já executada

- Spec integral de criação, overlay de ativação e policy:
  [andy_visitor_recovery.proposed.json](../../config/examples/andy_visitor_recovery.proposed.json).
- Preflight puro:
  [andy_visitor_recovery_preflight.py](../../scripts/forensics/andy_visitor_recovery_preflight.py).
  Não aplica mudanças e sempre declara operational_approval_granted=false.
- SHA256 canônico do manifesto:
  `c22cdaef3702407f2236614db9652a440e93a8c725f2e758b9fe3cbccb5443df`.
  Criação `80d569ffcb56df54efd072f3b202d1338a1340f91eccbe89e070181134cafada`;
  ativação `dd34694b3a7aaa187aba830115320f557cb4d009158f6b277d8d8507785f11b1`;
  policy `444775fee3bced1e7c4a4c48defb655ea81b7abe6f01022ecbcb2f7dfa296c44`.
- 18 cenários SQLite do candidato e15 testes do preflight passaram; rodada
  consolidada com harness/contratos existentes:41 PASS em5,79s. Os17 cenários
  com DB também estão parametrizados para PostgreSQL distribuído. Resultado do
  SHA final nos checks da PR, sem tratar PASS SQLite como PASS PostgreSQL.
- Snapshot privado em leitura do Worker alvo, inspect e6 arquivos de composição/
  env, com hashes/permissões, preparado no checkpoint. Não é autorização para
  aplicar o pacote e deve ser renovado/verificado na janela de promoção.

```bash
python -m scripts.forensics.andy_visitor_recovery_preflight \
  config/examples/andy_visitor_recovery.proposed.json \
  --synthetic-tenant-match --synthetic-version-valid
```

Essas flags simulam validações de escopo/versão para ensaio. Não comprovam tenant
real, versão produtiva, aprovação humana ou publicação. O checksum canônico usa
o stable_hash existente, não o hash dos bytes formatados do arquivo JSON.

## Preflight: artefatos exigidos antes de escrever

1. Fixar o SHA do Worker alvo, hashes dos arquivos e lista **exata** de Compose
   files carregados. Capturar os settings efetivos: pipeline, agent, execution,
   autonomous, external delivery, default presente/ausente, activation watermark,
   max age e legacy fallback. Comparar com baseline aprovado.
2. Usar a spec integral já validada pelo schema existente em ambiente descartável,
   sem LLM/transport. Registrar checksum e nova versão proposta. Rejeitar domínio
   diferente de general, outro tenant, versão inválida ou mistura de artefatos
   de teste. O selector atual não faz essas verificações de domínio/status.
3. Determinar a policy que o visitante realmente seleciona. Verificar
   `conversational_execution={execution_mode:AUTO_ALLOWED,
   allowed_actions:[respond,request_information]}` ou contrato equivalente já
   existente; capabilities/disclosures devem continuar negados. Não editar
   policy de contatos conhecidos só para passar o teste.
4. Executar os17 cenários do relatório com a **spec e policy do candidato**,
   generator sintético e adapters sem rede. Esses cenários foram preparados e
   executados em SQLite; PostgreSQL deve confirmar persistência/rollback no SHA.
5. Reproduzir com fixtures sintéticas em PostgreSQL descartável no CI03 e executar Python/Docker/Transport
   e architecture guard no SHA da ferramenta/código eventualmente necessário.
   Registrar testes e checks associados ao mesmo SHA, sem herdar PASS antigo.
6. Fazer dry-run de criação/publicação e rollback em banco descartável. Verificar
   que não muda identidade, vínculo, tenant, controles, grants, cursors, Relay,
   MAESTRO ou versões históricas. Uma falha exige rollback integral do candidato.

Só após esses artefatos o operador recebe a solicitação de promoção operacional.
Esta investigação já autoriza prepará-los, mas não autoriza publicar/autonomizar
o blueprint nem escrever seu ID no Worker produtivo. As lacunas de testes estão
registradas na matriz, sem alegação de recuperação certificada.

## Snapshot privado e mudança operacional proposta

Executar em janela aprovada, sem expor valores em argv/transcript:

1. Diretório privado timestampado0700, arquivos0600; snapshot dos settings
   allowlisted e cópias dos Compose/env realmente consumidos. Registrar SHA256,
   permissões, image digest/OCI revision e container ID. Arquivos contendo
   credenciais permanecem privados. Não publicar o inspect completo.
2. Backup PostgreSQL privado consistente por procedimento existente, com
   verificação de restauração em ambiente privado isolado, sem expor dados em
   logs/artifacts de CI. Adicionalmente registrar
   snapshot das linhas de blueprint/versão/policy envolvidas e seus checksums;
   bloquear se tenant, current_version ou policy mudarem desde o preflight.
3. Materializar o novo blueprint/versão pelo caminho administrativo revisado,
   com created_by/change_reason/approval vinculados ao procedimento. Aprovar
   explicitamente o checksum, status e execution_mode. Nenhuma mutação em
   binding/identidade; versões anteriores permanecem imutáveis.
4. Adicionar o ID aprovado ao **manifesto/overlay persistente existente** do Worker,
   após backup. Renderizar a composição completa em arquivo privado e comparar
   gates/proveniência; não substituir12 overlays por um Compose mínimo novamente.
   O único valor funcional pretendido nessa etapa é o default; os gates atuais
   já habilitados não são ampliados globalmente. Nenhuma policy adicional sem
   delta aprovado e seu próprio snapshot.
5. Recriar exclusivamente o Worker com mesma imagem, redes e mounts, pela
   composição completa já validada. Isso exige a aprovação da janela; não é um
   comando a executar nesta investigação. Conferir settings carregado após start.
6. Não replayar entradas antigas: usar mensagem nova da identidade de teste
   autorizada, depois uma segunda identidade consentida para excluir solução
   específica por número. Só realizar provider/WhatsApp real na janela autorizada.

Não se fornece comando Compose abreviado com valores inventados: a lista viva
inclui overlays privados e precisa ser fixada integralmente no artefato aprovado.
O procedimento preserva essa lista como condição verificável.

## Aceitação e rollback

Para cada novo canário autorizado: entrada externa → seleção explícita do
blueprint general → SDK → policy/autonomia textual → intent único → outbox
única → despacho; registrar ACK/ledger de entrega separadamente. Confirmar
capability intents=0, Gmail calls=0, owner_authenticated=false, nenhum dado
privado no contexto/proposta, e isolamento de tenant. Falta de tracing não deve
interromper a função; tracing causal ausente fica NOT_PROVEN.

Rollback se houver domínio/tenant incorreto, chamada de capacidade inesperada,
divulgação privada, duplicata, falha do caminho ou drift de gates:

1. Restaurar apenas o overlay/config do Worker do snapshot e recriar somente
   esse serviço com a mesma imagem. Conferir default e demais gates contra o
   snapshot; não restaurar indiscriminadamente banco ou controles globais.
2. Pelo procedimento administrativo revisado, retirar o novo candidato da
   seleção/publicação, preservando versões e auditoria. Não apagar intents,
   outboxes ou evidências; mensagens enviadas não são reversíveis.
3. Outbox/intent pendente exige tratamento pelo mecanismo existente de
   cancelamento/reconciliação, preservando deduplicação, idempotência e estados ambíguos.
   Não repetir envio para testar se o primeiro chegou.
4. Verificar que nenhum serviço vizinho, Relay, MAESTRO, Zabbix, Gmail ou cursor
   mudou. Atualizar STATUS, issue e checkpoint com o resultado real.

## O que falta para autorizar a promoção

Spec integral/checksum e regressões sintéticas estão preparados. A branch entrega
prova causal, candidato limitado e testes; certificação distribuída do SHA,
validação de tenant/versão reais e o caminho administrativo revisado continuam
condições de promoção. Nenhum PASS sintético autoriza escrever no runtime.
A decisão operacional deve abranger publicação, aumento explícito da autonomia
textual, default e eventual delta da policy, incluindo alcance non-direct.
