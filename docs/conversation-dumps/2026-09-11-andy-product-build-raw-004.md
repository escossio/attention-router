# Raw conversation checkpoint — Andy product/build

> Preservação bruta do bloco seguinte de 10 mensagens relevantes após o checkpoint raw-003. Sem síntese, consolidação ou decisão arquitetural adicional.

## 1 — ASSISTANT

Vamos — **e a obra já andou.**

Primeiro, fechei e fiz **squash merge do PR #34**, então o **Artifact Plane V0 já está na `main`**, no commit `1e7e0f0c4b7b9a2f359f57c128672cc9f0fae7f6`.

Em seguida comecei imediatamente o próximo tijolo: **Explicit Tenant Runtime V0**. Abri o **PR #35 — `feat: require explicit tenant at local runtime ingress`**.

Nesse PR, o WhatsApp local agora carrega explicitamente `tenant_id`; evento e mídia levam esse tenant no próprio payload assinado por HMAC; o ingresso rejeita requisição sem tenant; confirma no banco que o tenant existe e está `ACTIVE`; e replay, conflito e mídia passam a ser procurados dentro daquele tenant. Também coloquei fail-closed: se o transporte estiver configurado para encaminhar mensagens mas não tiver `LOCAL_TENANT_ID`, ele **não encaminha nada**.

O primeiro CI encontrou exatamente um teste antigo cujo fixture habilitava forwarding sem informar tenant. Em vez de contornar a nova regra, corrigi o fixture e acrescentei um teste específico provando que **forwarding sem tenant → `blocked_missing_tenant`**.

A segunda rodada já avançou bastante: **CodeQL verde, secret-scan verde e transport-tests verdes**. Python, Docker e PostgreSQL ainda estavam executando quando acabou minha janela de interação com as ferramentas, então **não fiz merge do #35 ainda**. Esse é o único ponto pendente.

E deixei deliberadamente uma transição segura: alguns chamadores internos antigos ainda podem usar o tenant padrão, mas **a fronteira HTTP autenticada de produção não pode**. Está documentado no próprio PR como compatibilidade temporária, para não tentarmos arrancar todos os `DEFAULT_TENANT_ID` do repositório numa única mudança arriscada.

Assim que o #35 ficar completamente verde, o próximo tijolo é o que queríamos desde essa conversa:

**Personal Context V0 — começar a representar tecnicamente “quem é esta pessoa e o que a Andy sabe sobre ela”.**

## 2 — USER

Finalizou

## 3 — ASSISTANT

Sim — **os checks finalizaram todos verdes**: **Public CI = success** e **CodeQL = success**.

O **PR #35 está aberto e mergeável**, mas **ainda não foi incorporado à `main`**.

Então tecnicamente: **implementação e validação concluídas; falta só o merge final.**

## 4 — USER

Vamos seguir

## 5 — ASSISTANT

Seguimos — e já avançamos para a próxima fileira.

O **PR #35 foi mergeado na `main`** por squash, commit `80f0bdd9...`. Então o **tenant explícito no runtime local já virou fundação oficial**.

Na sequência já construí o **Personal Context V0** e abri o **PR #36 — `feat: add tenant-scoped Personal Context V0`**. Ele agora resolve quem é a pessoa representada pela Andy dentro do tenant, aceita várias identidades de origem — por exemplo WhatsApp + e-mail — quando todas apontam para a mesma pessoa, reaproveita `MemoryClaim` e `Fact` existentes, preserva confiança/proveniência/validade e impede vazamento entre tenants.

Também coloquei uma regra que considero fundamental: **Personal Context é conhecimento, não autoridade**. A Andy saber seu endereço, Pix ou empresa não significa que esteja autorizada a contar isso para alguém. Por enquanto esse Personal Context é **read-only e nem sequer entra automaticamente no prompt do motor**. Primeiro construímos o cofre; depois construímos corretamente a porta.

Já há testes cobrindo múltiplos canais para o mesmo dono, isolamento entre tenants, exclusão de segredo, claims expirados/inativos, fatos estruturados e falha fechada quando não conseguimos determinar inequivocamente quem a Andy representa.

No CI atual do #36, **CodeQL, transport-tests e secret-scan já estão verdes**; Python, PostgreSQL e Docker ainda estavam terminando na última consulta. Portanto, **não fiz merge do #36 ainda**.

E mantive nossa disciplina de preservação: o próximo lote bruto de 10 mensagens também foi salvo como **`raw-003`**, sem destilação.

Agora estamos literalmente chegando ao ponto em que podemos representar tecnicamente:

**“Esta é a Andy do tenant X. Esta é a pessoa que ela representa. Isto é o que ela sabe sobre essa pessoa. Isto é de onde cada conhecimento veio.”**

O próximo tijolo depois desse fechar verde é o **Context Retrieval** — decidir *qual pequena parte desse conhecimento é relevante para a situação atual*, sem despejar a vida inteira do usuário no modelo.

## 6 — USER

A respeito da integração com os outros canais qual melhor forma de fazermos, SDK?

## 7 — ASSISTANT

**SDK faz muito sentido**, mas eu colocaria uma condição importante: **não começar pelo SDK; começar pelo contrato.** O SDK seria a implementação conveniente desse contrato.

Para a Andy, eu desenharia assim:

> **Provider → Connector/Adapter → Integration SDK → contrato canônico → Attention Router**

O Attention Router **não deveria saber o que é Gmail, Telegram, WhatsApp, Outlook ou aplicativo Android**. Ele deveria receber sempre a mesma coisa: “chegou um evento deste tenant, deste ator, neste instante, com este conteúdo e estes artifacts”.

Aliás, já temos um embrião muito bom disso: o projeto possui `CanonicalEvent`, `ContextSnapshot`, tenant e uma camada de capabilities. Então não estamos inventando outra arquitetura; estamos criando **a porta padronizada para entrar e sair dela**.

## Eu separaria duas coisas

**Channel SDK** para lugares onde a Andy conversa/recebe eventos:

WhatsApp, e-mail, Telegram, SMS, aplicativo Android, webchat, Teams, Slack etc.

E **Capability SDK** para serviços que a Andy utiliza para fazer algo:

Google Calendar, Drive, Maps, localização, Home Assistant, sistemas empresariais, CRM etc.

Essa distinção vai evitar uma bela macarronada daqui a dois anos. 😄

Por exemplo:

**Gmail como canal**

“João enviou um e-mail com uma planilha.”

É entrada de evento.

**Google Calendar como capability**

“Marque uma reunião com João amanhã às 14h.”

É uma capacidade acionada pela Andy.

Mesmo sendo ambos Google, arquiteturalmente são papéis diferentes.

---

## O Channel SDK

Eu faria o contrato central prever coisas como:

`tenant_id`  
`source`  
`source_account`  
`external_event_id`  
`actor`  
`conversation/thread`  
`timestamp`  
`content`  
`attachments/artifacts`  
`reply_reference`  
`correlation_id`  
`metadata`

Então um conector Gmail recebe aquela monstruosidade específica da API do Google e converte para:

**CanonicalInboundEvent**

O WhatsApp faz exatamente a mesma coisa.

Telegram também.

A partir daí:

**o motor da Andy não sabe mais por onde aquilo chegou.**

Isso é exatamente o que precisamos para aquele cenário que você deu:

Planilha chegou pelo WhatsApp na segunda.

Planilha chegou por e-mail na terça.

No núcleo:

**Artifact A**  
Receipt 1 → WhatsApp  
Receipt 2 → Gmail

Pronto.

---

## E a saída segue o mesmo princípio

O motor também não chama diretamente:

`gmail.send()`

ou:

`whatsapp.sendMessage()`

Ele produz algo canônico:

**ExecutionIntent**

Algo como:

“Responder este ator nesta conversa usando o canal associado.”

Aí:

**Integration SDK → Connector WhatsApp → WhatsApp**

ou

**Integration SDK → Connector Gmail → Gmail**

Então conseguimos trocar o transporte sem mexer no cérebro da Andy.

Essa separação é ouro.

---

## Mas eu faria o SDK multi-linguagem de um jeito diferente

Aqui tem uma pegadinha.

Nosso core é muito Python.

Nosso transporte WhatsApp atual tem Node.js.

Amanhã algum connector pode ser Go, Kotlin, TypeScript...

Portanto eu **não faria o contrato existir somente como uma biblioteca Python**.

Eu faria:

**Contrato = linguagem neutra**

Por exemplo:

JSON Schema / OpenAPI para chamadas síncronas  
schemas versionados para eventos  
contratos de erro  
idempotência  
autenticação  
artifact references

E por cima:

**Andy Integration SDK Python**

**Andy Integration SDK TypeScript**

eventualmente outras.

Assim, o contrato é a Constituição.

O SDK é só a tradução dela para uma linguagem.

---

## O SDK deveria resolver a parte chata

Um conector novo não deveria precisar reinventar:

assinatura/autenticação, tenant, idempotência, retry, backoff, correlation IDs, upload de artifacts, hash SHA-256, receipts, observabilidade, erros padronizados, healthcheck e versionamento de contrato.

O desenvolvedor de um futuro connector deveria praticamente escrever só:

> “Como recebo eventos deste provider?”

> “Como envio uma ação para este provider?”

Todo o resto deveria vir do SDK.

Isso é uma ótima forma de escalar inclusive para terceiros no futuro.

Imagine alguém querer criar:

**Andy Connector for TOTVS**

Ele não precisa conhecer as entranhas do Attention Router.

Instala nosso SDK, implementa os contracts, declara as capabilities e acabou.

Aí nossa frase volta com força:

> **A Andy não deveria tentar ser o lugar onde tudo acontece. Ela deveria ser o lugar que sabe fazer tudo acontecer.**

O SDK seria literalmente **a tomada onde o mundo conecta na Andy**.

---

### O que eu construiria agora

Antes de integrar Gmail, Telegram ou qualquer outro segundo canal, eu criaria o **Integration Contract V0**.

Pouco, firme e genérico:

**Inbound Event Contract → Artifact Contract → Identity Contract → Outbound/Execution Contract → Error/Retry Contract.**

Depois usamos **dois adapters reais para provar o desenho**:

WhatsApp, que já existe;

e **e-mail**, porque ele força problemas bem diferentes: threads, assunto, remetente, anexos, múltiplos destinatários etc.

Se WhatsApp **e** e-mail conseguem usar o mesmo contrato sem colocar `if gmail` ou `if whatsapp` dentro do Attention Router, encontramos uma parede muito boa.

Depois extraímos isso formalmente como **Andy Integration SDK**.

Então minha resposta é:

**Sim, SDK. Mas arquitetura “contract-first, SDK-second, adapters-third”.**

E eu considero isso mais um **tijolo de fundação**, não pintura. Porque ele determina se a Andy vai crescer para 3 integrações ou para 300 sem transformar o núcleo num Frankenstein.

## 8 — USER

<<ImageDisplayed>>

## 9 — ASSISTANT

Sim — **essa tela confirma o que a gente queria ver**.

O **PR #36, Personal Context V0**, terminou com **CodeQL verde e Public CI verde**. A execução anterior que aparece com ícone cinza/fracasso na imagem é histórica; ela foi **superada pela execução mais nova que passou**. Consultei o estado atual e o PR continua **aberto, mergeável e com os dois workflows concluídos com sucesso**.

Ou seja: **Personal Context V0 está pronto para merge.**

Se quiser, seguimos agora e eu faço o merge do #36 e já puxo o próximo tijolo: **Context Retrieval V0**.

## 10 — USER

Sim vamos
