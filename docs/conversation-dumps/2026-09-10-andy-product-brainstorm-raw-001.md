# Raw conversation checkpoint — Andy product brainstorming

> Preservação bruta das 10 mensagens anteriores à solicitação de arquivamento. Sem síntese, consolidação ou decisão arquitetural adicional.

---

## 1 — USER

Perfeito, é isso aí. Eu imaginava que tu ia caminhar na mesma linha, pois a gente já conversou sobre isso muitas vezes. A ideia da Andy é ser algo criado, algo desenvolvido, algo que cria-se uma personalidade no decorrer do uso. Perfeito, é isso mesmo. A respeito de pensando em produto, as escolhas que o usuário vai dando, o contexto de aprendizado. Quando você fala aprendizado da Andy, quando você fala em aprendizado, eu tô pensando assim: vendi o software para uma pessoa. A pessoa começa a utilizar. O software ele vai ser praticamente uma aplicação, quanto mais burra melhor. Compreendo isso, isso é bem claro. aquelas informações de contexto, elas iriam ser criadas para cada usuário que aderissem ao serviço. Nesse contexto, o que iria ter? Quando você fala aprendizado de personalidade, eu não sei por que que Machine Learning é algo tão interessante e a gente nunca implementou em projetos, mas aprendizado estaria separado em um módulo do motor que causaria o aprendizado.

---

## 2 — ASSISTANT

**Sim — eu separaria o aprendizado do motor de execução.** E acho que aqui aparece uma divisão arquitetural muito importante para a Andy como produto.

A Andy que você vende para alguém deveria nascer com um **motor genérico e conservador**, praticamente sem saber nada daquela pessoa. Ela sabe conversar, interpretar contexto, reconhecer intenções, aplicar regras de segurança e pedir autorização. Mas **ela ainda não conhece o dono**.

O que vai crescendo com o uso não é o código da Andy. É um **modelo pessoal daquele usuário**.

Eu dividiria em três coisas bem diferentes:

1. **Motor da Andy** — é o software. Interpreta mensagem, contexto, intenção, decide o que fazer, executa ações. Esse motor é igual para todos e muda através das nossas versões do produto.
2. **Modelo pessoal do usuário** — é tudo que a Andy aprendeu sobre aquela pessoa: informações, preferências, estilo, limites, relações, permissões, hábitos, formas de responder etc.
3. **Motor de aprendizado** — observa o que acontece e propõe alterações no modelo pessoal. Ele não responde WhatsApp; ele aprende com o comportamento.

Essa separação é fundamental porque eu **não deixaria a Andy “aprender” alterando diretamente suas próprias regras de execução**. Isso seria perigoso e difícil de auditar. O aprendizado produz uma informação nova; o motor decide se aquela informação já tem autoridade suficiente para ser usada.

Por exemplo, alguém pede o Pix. Você aprova. O módulo de aprendizado registra algo como: “Leonardo autorizou compartilhar `payment.pix.primary` neste contexto”. Na segunda, terceira, quarta vez, ele percebe a repetição. Então pode nascer um candidato:

> “O dono costuma autorizar o compartilhamento do Pix quando solicitado diretamente.”

Mas isso ainda não precisa virar autorização automática. O sistema pode aumentar uma confiança interna e depois perguntar: **“Você já autorizou isso várias vezes. Quer que eu faça automaticamente daqui para frente?”**

Aí, quando você confirma, não aconteceu “Machine Learning mágico”. Aconteceu algo muito mais controlável: **o módulo de aprendizado converteu histórico em uma nova regra explícita do modelo pessoal**.

E aqui está a parte interessante: **Machine Learning pode entrar dentro desse módulo depois**, mas ele não precisa ser a fundação.

No começo, eu faria o aprendizado quase todo determinístico. Eventos como aprovação, rejeição, edição de resposta, resposta manual, repetição de comportamento e correções viram sinais. O Learning Engine interpreta esses sinais e cria candidatos a aprendizado.

Então poderíamos ter, por usuário:

- **Facts/Vault**: Pix, e-mail, endereço comercial, links, dados da empresa etc.
- **Preferences**: “prefere respostas curtas”, “não gosta que falem que está ocupado”.
- **Disclosure rules**: Pix pode sair automaticamente; endereço residencial exige aprovação.
- **Behavior profile**: nível de informalidade, humor, vocabulário, tamanho de resposta.
- **Relationship/context model**: como costuma interagir com determinados contextos ou relações.
- **Decision history**: o que aprovou, negou, corrigiu ou respondeu manualmente.
- **Learned rules**: conclusões consolidadas derivadas desse histórico.

E cada item aprendido deveria ter **origem, evidência, confiança, data e possibilidade de reversão**. Esse detalhe é importantíssimo. A pessoa poderia perguntar para a Andy:

> “Por que você começou a mandar meu Pix automaticamente?”

E a Andy conseguiria responder conceitualmente: “Porque você autorizou esse compartilhamento várias vezes e depois permitiu que eu fizesse isso automaticamente.”

Isso é muito diferente de um modelo simplesmente “achar” alguma coisa.

### Onde entra Machine Learning

Aí sim fica interessante.

Machine Learning poderia atuar em coisas como reconhecer que:

“Me passa tua chave”
“Qual teu Pix?”
“Pra onde eu transfiro?”
“Me manda os dados pra pagamento”

pertencem ao mesmo **contexto semântico `REQUEST_PAYMENT_INFO`**.

Também poderia descobrir padrões de estilo: você quase sempre apaga introduções longas, muda “Olá” para “Oi”, prefere respostas de duas frases, costuma brincar com certas pessoas etc.

Mas eu trataria ML como **um componente do Learning Engine**, não como o Learning Engine inteiro.

Em arquitetura eu gosto muito de chamar isso de dois planos:

**Execution Plane** → decide e executa agora.

**Learning Plane** → observa o que aconteceu e modifica lentamente o modelo pessoal usado nas próximas decisões.

Isso inclusive permite que um falhe sem destruir o outro.

Se o módulo de aprendizado parar por uma hora, a Andy continua funcionando normalmente com tudo que já sabia. Se o motor de execução reiniciar, o aprendizado acumulado continua persistido.

E tem uma ideia ainda mais forte aqui: **cada instalação da Andy começa praticamente igual, mas nenhuma permanece igual depois de alguns meses.**

O software continua sendo nosso.

Mas a **Andy daquela pessoa** começa a se tornar dela.

É aí que “personalidade desenvolvida pelo uso” deixa de ser somente uma ideia bonita e começa a virar uma arquitetura concreta.

---

## 3 — USER

Antes até mesmo de ler a tua resposta, não li ela ainda. Eu vou logo adiantar essa outra questão. Vou te dar o exemplo aqui de uma empresa grande que ela segue essa linha e eu não acho essa linha interessante. Por exemplo, LG. A LG, ela possui eletrodomésticos proprietários e uma aplicação que roda a parte de automação. Só que a LG, até pouco tempo, não sei como é que tá agora, ela dificultava muito as integrações com o próprio Google. Por exemplo, eu quero mandar liga, ok Google, aí eu quero que ela pegue e ligue uma TV. Google, ligue uma TV, essa TV é da LG. Então o Google teria que mandar a informação pra LG e a LG teria que gerar aquele acionamento. Agora a pergunta é: a dificuldade daquela automação era uma limitação da LG, no intuito de criar uma infraestrutura própria e criar uma rede com o Google, forçar as pessoas a utilizarem mais ainda, ou aquilo dali era uma limitação do Google à LG? Essa é uma grande pergunta. Porque eu imagino que a resposta seja uma limitação que a própria LG tinha interesse em ter, para forçar os usuários a utilizarem os aplicativos dela e os produtos dela. E eu acho que era uma limitação da LG e eu acho que não vai ser o nosso caso. Implementação de agenda, localização, muita coisa interagindo direto com o Google seria bastante interessante.

---

## 4 — ASSISTANT

Sim — e essa tua leitura tem **muito valor para a arquitetura da Andy**, mas eu faria uma correção importante: no caso LG × Google, **não dá para atribuir toda a limitação à LG como estratégia deliberada de prender o usuário**. Tecnicamente, as duas pontas impunham limites.

O funcionamento clássico era mais ou menos assim:

**“Ok Google, ligue a TV” → Google entende a intenção → chama a integração LG ThinQ → nuvem da LG decide/executa → TV recebe o comando.**

O Google não estava falando diretamente com a televisão. A LG precisava expor ao Google quais dispositivos existiam e quais capacidades cada um oferecia. E é aí que a tua suspeita encontra bastante fundamento técnico: **a LG controlava uma parte enorme da superfície disponível para integração**.

A própria documentação da LG deixa isso evidente. Para o Google Assistant, ela dizia que somente as funcionalidades básicas eram oficialmente suportadas em determinados modos e que certas funções do ThinQ simplesmente não estavam disponíveis por comando de voz. Inclusive havia dois modelos de integração: um `Direct Action`, mais simples, e outro que passava pela própria LG e oferecia mais funcionalidades.

Então poderia acontecer exatamente isto:

**LG ThinQ:** 25 funções disponíveis.  
**LG → Google:** 8 funções expostas.

Nesse cenário, não adiantava o Google ser capaz de compreender “faça X”. Se a integração da LG não oferecesse `X`, acabava ali.

Mas existia o outro lado. O Google também definia os tipos de dispositivo, traits, operações permitidas, regras de segurança, autenticação, comandos de voz disponíveis etc. E essas plataformas mudavam ao longo do tempo. A própria LG alerta que comandos e recursos do Google poderiam ser alterados pelo Google.

Portanto, eu resumiria como:

**limitação percebida pelo consumidor = capacidade da plataforma Google ∩ aquilo que a LG decidiu/implementou expor.**

E é aí que entra uma decisão de produto que eu acho **fundamental para a Andy**:

> **A Andy não deveria tentar ser o lugar onde tudo acontece. Ela deveria ser o lugar que sabe fazer tudo acontecer.**

Isso muda completamente nossa filosofia.

Eu não quero que uma pessoa tenha que abandonar o Google Agenda para usar “Agenda Andy”. Não quero criar “Andy Drive”, “Andy Maps”, “Andy Contacts”, “Andy E-mail” só porque podemos.

Se ela já usa Google Calendar, a Andy usa o Calendar.

Se usa Gmail, a Andy trabalha com Gmail.

Se usa Google Drive, acessa o Drive.

Se usa um calendário Microsoft, futuramente usamos Microsoft.

Se usa Home Assistant, falamos com Home Assistant.

Se a fechadura possui uma API decente, integramos a fechadura.

A **personalidade, memória, contexto e decisão são da Andy**. Os serviços externos são capacidades.

Arquiteturalmente, isso ficaria lindo porque conversa diretamente com o que estávamos discutindo antes:

**Context Engine → Decision Engine → Capability Registry → Connector/Adapter → serviço externo.**

Então a Andy poderia concluir:

“Leonardo tem uma reunião às 15h.”

Ela não precisa possuir um calendário. Ela pergunta ao módulo `calendar` e o adapter daquele usuário pode ser `GoogleCalendarAdapter`.

Outro usuário poderia ter:

`MicrosoftCalendarAdapter`.

Para o cérebro da Andy, ambos oferecem algo conceitualmente parecido:

`calendar.get_events()`  
`calendar.create_event()`  
`calendar.find_free_slot()`

Isso é inclusive muito próximo daquela ideia que já tivemos do **SDK comum entre nossos projetos**.

### E localização merece uma distinção interessante

“Google” não precisa necessariamente ser o proprietário da localização da Andy.

No Android, se o aplicativo estiver no celular, podemos usar as APIs de localização do próprio sistema, com as permissões concedidas pelo usuário. Google Maps pode entrar para mapa, endereço, rota, geocodificação etc.

Então a Andy poderia saber:

**“onde estou?”** → dispositivo / Android.

**“que lugar é esse?”** → serviço de mapas/geocodificação.

**“quanto tempo daqui até o aeroporto?”** → Maps/rotas.

São capacidades separadas.

E isso evita uma coisa que eu considero uma armadilha de produto: criar dependência desnecessária de um único fornecedor.

Eu chamaria essa filosofia da Andy de **integration-first**.

Ela teria seu núcleo proprietário — personalidade, memória, contexto, segurança, decisão e aprendizado — mas uma camada de integração **deliberadamente aberta**.

A LG construiu uma casa inteligente e depois abriu algumas janelas para os outros.

Eu prefiro pensar a Andy ao contrário:

**ela nasce cheia de portas.**

E quanto mais serviços ela conseguir usar sem obrigar a pessoa a mudar a vida digital que já possui, **mais invisível a Andy fica**. Para uma assistente pessoal, invisibilidade é uma característica extremamente valiosa.

---

## 5 — USER

Primeira coisa, aquela frase lá de cima, a Ende não deveria tentar ser o lugar onde tudo acontece. Ela deveria ser o lugar que sabe fazer tudo acontecer. Considero uma das primeiras frases marcantes realmente que traz a ideia da Ende. Parece que a gente tá batendo papo, papo furado, mas não é, tá? Vamos bater mais papo furado, pra gente reunir o papo furado todo, filtrar e trazer o importante pra ser desenvolvido. Importante você trazer todas as colocações, que já foram muitas aqui, e ir criando um fluxograma a nível de hierarquia de projeto. No intuito de entender as coisas que nós estamos conversando, em que momento de implantação, porque a minha ideia desse papo aqui é saber que a gente precisa ter uma parede que chame bastante atenção e bem bonita futuramente. Mas antes a gente precisa subir os tijolos pra depois botar aquela pintura, aquela sensação de beleza e de grandeza que a gente pensa agora. Mas antes a gente precisa subir os tijolos, e não é bonito. Não é que não seja bonito, não é algo... é algo primordial, pois aquela beleza final ela jamais existiria sem os tijolos. Então é nos tijolos que eu estou interessado agora. Creio que depois que o muro tiver todo em pé e bem alto, essas ideias serão só o começo, muitas outras vão aparecer. Mas a gente precisa da ideia final pra saber o que nós precisamos levantar de muro, se é um muro de 10 metros ou um muro de 20 metros, tá? Eu vou levantar mais informação também, vá levantando mais informação caso tenha.

---

## 6 — ASSISTANT

É exatamente isso. Esse “papo furado” é, na prática, **engenharia de produto antes da engenharia de software**. Se a gente começar a codificar cedo demais, corremos o risco de construir um muro impecável de 10 metros e descobrir depois que a casa precisava de um de 20.

E eu quero transformar uma coisa que você acabou de definir em regra nossa: **a visão final serve para dimensionar a fundação, não para antecipar a pintura.** A interface bonita, a experiência cinematográfica, os cartões de aprovação, animações, voz perfeita, tudo isso interessa — mas serve agora principalmente para nos dizer **quais capacidades precisam existir por baixo**.

A frase que você destacou merece ficar no topo da hierarquia:

> **A Andy não deveria tentar ser o lugar onde tudo acontece. Ela deveria ser o lugar que sabe fazer tudo acontecer.**

A partir dela, eu já enxergo nosso mapa assim:

1. **Visão do produto — “quem é a Andy?”**  
   Não é um chatbot e não é uma coleção de automações. É uma representação operacional da pessoa, capaz de compreender contexto, conhecer seu dono, utilizar serviços externos e agir dentro da autoridade que recebeu.

2. **Attention Router — a fundação estrutural.**  
   Aqui ficam eventos, identidade, estado, decisão, políticas, intents, aprovação, execução, outbox, transporte, auditoria, idempotência e segurança. É concreto armado. O usuário final nem deveria saber que isso existe.

3. **Modelo pessoal — “quem é este usuário?”**  
   Cada cliente começa quase vazio. Aos poucos surgem fatos, preferências, relações, estilo, limites, dados compartilháveis, hábitos, histórico de decisões e conhecimento pessoal. O software é igual; **a Andy resultante é diferente para cada pessoa**.

4. **Compreensão contextual — “o que está acontecendo agora?”**  
   É diferente de simplesmente classificar quem falou. “Está solteiro?” é conversa sobre informação pessoal. “Manda teu Pix” é solicitação de dado para uma ação financeira. “Pode confirmar a reunião?” envolve compromisso. Aqui nasce aquela mudança que acabamos de perceber: **o contexto, e não o público, determina a natureza da decisão**.

5. **Autoridade e divulgação — “posso fazer isso?”**  
   Depois de entender a intenção, a Andy verifica o objeto envolvido. Pix pode ser `AUTO_SHARE`; endereço pode ser `ASK_OWNER`; senha pode ser `NEVER_SHARE`. Essa camada separa conhecimento de permissão: **saber uma informação não significa poder divulgá-la**.

6. **Capabilities — “como faço isso no mundo?”**  
   Agenda, e-mail, localização, pagamentos, documentos, casa inteligente, CRM, WhatsApp, serviços Google etc. A Andy não recria esses produtos. Ela possui uma linguagem comum de capacidades e adapters conectam os provedores. O cérebro pede “criar compromisso”; hoje pode executar no Google Calendar, amanhã em outro calendário.

7. **Learning Plane — “o que devo aprender com isso?”**  
   Fica separado da execução. Observa aprovações, negativas, correções, respostas manuais, recorrências e escolhas. Produz candidatos a aprendizado e, conforme a autoridade necessária, incorpora isso ao modelo pessoal. Aqui Machine Learning pode entrar depois como instrumento, **não como autoridade soberana**.

8. **Delegação progressiva — “até onde posso agir sozinha?”**  
   A Andy nasce conservadora. Conforme o usuário a ensina, determinadas ações passam de “perguntar sempre” para “sugerir automatização” e eventualmente “agir automaticamente”. A autonomia é conquistada por histórico e consentimento, não presumida.

9. **Experiência — a pintura.**  
   É onde entram a aprovação bonita, aplicativo agradável, interação por voz, animações, apresentação de contexto, configuração simplificada etc. Ela será importantíssima comercialmente. Mas quando chegarmos aqui, a tela deverá estar apenas **expressando primitivas que já existem no motor**, e não inventando regras de negócio dentro do aplicativo.

Isso já muda como eu avaliaria qualquer ideia nova nossa. Quando você disser, por exemplo, “a Andy deveria perceber que estou dirigindo e responder diferente”, eu não começaria pensando na tela que mostraria isso. Eu classificaria: isso exige **Context → Presence/Activity → Personal Model → Decision**. A tela vem depois.

E tem algumas consequências novas que eu colocaria desde já no radar dos **tijolos**, porque elas podem alterar o tamanho da fundação.

Uma é **proveniência**. Tudo que a Andy “sabe” deveria saber também **de onde veio**. “O usuário digitou”; “foi inferido”; “veio do Google Calendar”; “foi observado cinco vezes”; “foi confirmado explicitamente”. Isso vai ser fundamental quando o aprendizado ficar grande.

Outra é **confiança separada de autoridade**. A Andy pode ter 98% de confiança de que descobriu seu novo endereço e ainda assim ter **0% de autoridade para armazená-lo como endereço oficial ou compartilhá-lo**. Essa distinção vai nos poupar muita dor futura.

Outra é uma espécie de **compilador de comportamento**. O Learning Plane pode acumular uma quantidade enorme de sinais, mas o Execution Plane não deveria consultar dez mil lembranças antes de cada mensagem. Periodicamente, esse conhecimento pode ser consolidado em um perfil operacional pequeno e versionado: “é assim que esta Andy deve se comportar atualmente”. Isso mantém velocidade, auditabilidade e possibilidade de rollback.

E tem uma que eu considero particularmente forte para produto: **a Andy deveria conseguir explicar suas próprias decisões**. Não necessariamente mostrar logs técnicos, mas responder coisas como “por que você me perguntou antes de mandar isso?” — “porque era seu endereço residencial e você definiu que eu devo pedir autorização.” Ou “por que você mandou o Pix sem perguntar?” — “porque você autorizou que eu compartilhasse esse dado automaticamente quando solicitado para pagamento.”

Isso significa que **explicabilidade também é um tijolo**, embora a apresentação bonita dela seja pintura.

Então vou adotar mentalmente quatro etiquetas para tudo que surgir daqui para frente: **Visão**, **Parede**, **Tijolo** e **Pintura**. E também uma noção de dependência: *o que precisa existir antes de quê*. Dessa forma, quando juntarmos esse material, não teremos só uma lista de ideias; teremos **uma árvore de produto e uma ordem racional de construção**.

E pode continuar jogando ideia aparentemente distante — Google, carro, agenda, localização, pagamentos, casa, comportamento, voz, qualquer coisa. Neste momento elas são úteis justamente porque estão nos mostrando **qual é o tamanho do muro que precisamos calcular antes de continuar assentando tijolo**.

---

## 7 — USER

Seguinte, pra gente ter uma conversa mais direcionada, uma conversa onde a gente consiga tirar dela mais informações pra nossa construção. Eu tava pensando em a lista de possibilidades serem diferenciadas pra nosso entendimento, por categorias de pessoas. Vou tentar explicar melhor. Quais são as necessidades de uma pessoa que dá aula em um colégio, em dois colégios? Quais são as necessidades de uma pessoa que trabalha como personal trainer, dando cinco aulas por dia, pra cinco pessoas diferentes, em cinco endereços diferentes, recebendo quinzenalmente ou mensalmente, aonde cada pessoa tem um horário, aonde o pagamento tem uma variação por horário. Nesse mês eu só dei tantas horas de aula, no outro mês eu dei tantas horas de aula. Agora pra um estudante, agora pra uma pessoa, pra um idoso, agora pra um pai de família, em que ele quer controlar, ter controle sobre seus filhos, pra um contador, pra uma pessoa que vive dentro da rede social. Pronto. Dentro dessas características vamos setar todas elas pra gente começar a jogar as necessidades de cada um. Não que as necessidades da Indy sejam diferentes pra uma pessoa ou outra, e sim pra gente criar esse contexto, essas ideias, essas possibilidades. Assim eu creio que fica mais fácil da gente compreender o tanto que a gente pode criar e o que haverá necessidade de se a ideia de um precisa aumentar uma fileira de tijolo ou não. Essa é a ideia.

---

## 8 — ASSISTANT

É isso. E tem um detalhe que deixa essa metodologia ainda mais forte: **essas categorias não seriam “tipos de cliente da Andy”; seriam lentes para estressar a arquitetura**.

Uma mesma pessoa pode ser professora, mãe, estudante e criadora de conteúdo ao mesmo tempo. Então não criamos quatro Andys. Pegamos quatro contextos diferentes e perguntamos: **“o que a Andy precisaria saber, perceber e conseguir fazer para ser realmente útil aqui?”**

Eu criaria um artefato nosso chamado provisoriamente de **Mapa de Contextos Humanos**. A primeira versão já poderia ficar assim:

| Contexto/persona | Situações reais que aparecem | Necessidades que a Andy pode assumir | Tijolos exigidos |
|---|---|---|---|
| **Professor / professora** | Dois colégios, turmas diferentes, provas, reuniões, mensagens de pais, mudanças de horário | agenda, preparação de aula, lembretes, organização de turmas, comunicação, documentos, acompanhamento de pendências | calendário, contatos, documentos, contexto de trabalho, tarefas, comunicação |
| **Personal trainer** | Cinco alunos por dia, locais diferentes, horários variáveis, cancelamentos, mensalidade/quinzena, quantidade de aulas | agenda, deslocamento, confirmação, contagem de aulas, cobrança, Pix, fechamento mensal | calendário, localização, rotas, ledger financeiro, pagamentos, comunicação |
| **Estudante** | aulas, trabalhos, provas, matérias, grupos, documentos, prazos | planejamento, lembretes, estudo, organização de arquivos, acompanhamento de entregas | calendário, tarefas, documentos, memória, aprendizagem |
| **Idoso** | consultas, medicamentos, compromissos, familiares, deslocamentos, dificuldade tecnológica | lembretes simples, contato com familiares, orientação, compromissos, localização e assistência | calendário, comunicação, localização, rotinas, acessibilidade |
| **Pai/mãe de família** | escola, compromissos dos filhos, transporte, contas, autorizações, comunicação familiar | coordenação familiar, agenda compartilhada, localização autorizada, lembretes, pagamentos, avisos | identidade/relacionamentos, calendário, localização, permissões, comunicação |
| **Contador** | vários clientes, documentos, vencimentos, obrigações recorrentes, cobranças | acompanhar pendências por cliente, cobrar documentos, lembrar prazos, organizar arquivos | CRM leve, tarefas, calendário, documentos, comunicação, automações recorrentes |
| **Criador / pessoa de redes sociais** | mensagens, propostas, comentários, agenda de conteúdo, marcas, publicações | triagem, respostas, calendário editorial, captura de oportunidades, acompanhamento de parceiros | canais sociais, contexto, comunicação, calendário, CRM |
| **Autônomo / prestador de serviço** | clientes diferentes, orçamento, execução, pagamento, retorno | organizar clientes, agenda, propostas, cobrança, follow-up | CRM, calendário, financeiro, documentos, comunicação |
| **Vendedor** | leads, conversas, propostas, retornos, reuniões | lembrar follow-ups, priorizar leads, recuperar contexto, agendar reuniões | CRM, memória, calendário, comunicação |
| **Executivo / gestor** | reuniões constantes, e-mails, decisões, pessoas, aprovações | resumir, priorizar, preparar reuniões, acompanhar compromissos e decisões | e-mail, calendário, memória, tarefas, documentos |
| **Pessoa muito móvel** | carro, aeroportos, reuniões em lugares diferentes, compromissos encadeados | localização, tempo de deslocamento, avisos de atraso, rotas, reorganização de agenda | localização, mapas, calendário, contexto em tempo real |
| **Pessoa comum / vida pessoal** | contas, compras, amigos, família, consultas, compromissos, mensagens | lembrar, organizar, responder, compartilhar informações autorizadas, acompanhar coisas | praticamente o núcleo básico da Andy |

E aqui aparece uma coisa **muito importante para nosso método de construção**.

O personal trainer parece exigir um produto completamente diferente do professor. Mas quando desmontamos os dois, percebemos que grande parte da parede é a mesma.

O professor precisa de:

**agenda + pessoas + tarefas + comunicação + documentos.**

O personal precisa de:

**agenda + pessoas + localização + comunicação + pagamentos.**

Portanto, não precisamos construir “módulo professor” e “módulo personal trainer”. Precisamos construir primitivas suficientemente boas para que **a combinação delas produza os dois comportamentos**.

Esse pode virar nosso critério para decidir se uma ideia aumenta o muro.

Se surgir:

> “O personal precisa saber quantas aulas deu este mês.”

Talvez isso não exija uma nova fileira inteira. Pode ser simplesmente:

**evento + pessoa + data + serviço prestado + valor → agregação.**

Mas se surgir:

> “A Andy precisa perceber que o usuário está dirigindo e mudar automaticamente seu comportamento.”

Aí temos algo novo:

**Context Awareness / Presence.**

Isso pode realmente exigir uma nova fileira de tijolos.

A mesma coisa para filhos. “Avisar da aula amanhã” é calendário. Mas:

> “Minha filha saiu da escola e ainda não chegou em casa; me avise se passar de determinado tempo.”

Isso introduz:

**localização + relacionamento + autorização + geofence + condição temporal + monitoramento.**

Aí sim apareceu arquitetura nova.

### Eu acrescentaria uma camada acima das personas

Além de perguntar “quem é essa pessoa?”, perguntaríamos **que tipo de problema está acontecendo?**

Porque isso vai revelar as primitivas universais da Andy:

**Tempo** — quando algo acontece.  
**Pessoa** — com quem.  
**Lugar** — onde.  
**Dinheiro** — quanto, pago ou devido.  
**Informação** — o que pode ser conhecido ou compartilhado.  
**Comunicação** — quem falou o quê e quem precisa responder.  
**Compromisso** — o que alguém prometeu fazer.  
**Documento** — qual informação formal existe.  
**Estado** — o que está acontecendo agora.  
**Autoridade** — o que a Andy pode fazer sem perguntar.  
**Memória** — o que aconteceu antes.  
**Aprendizado** — o que deve mudar depois disso.

Agora começa a ficar muito interessante, porque talvez descubramos que **quase toda a vida cotidiana pode ser representada pela combinação de algumas poucas primitivas**.

A personal trainer chamada Ana, por exemplo, não precisaria de um “sistema de personal trainer”. Poderíamos representar:

**Pessoa:** cliente João.  
**Serviço:** treino pessoal.  
**Recorrência:** segunda e quarta.  
**Local:** academia X.  
**Preço:** R$ N por sessão.  
**Regra de cobrança:** mensal, considerando aulas realizadas.  
**Comunicação:** WhatsApp.  
**Pagamento:** Pix.  
**Regra:** confirmar aula algumas horas antes.

E aí a Andy consegue inferir coisas como:

“João cancelou duas aulas este mês.”  
“Você realizou sete sessões com João.”  
“Hoje deveria ocorrer a oitava.”  
“O fechamento mensal dele ficou em X.”  
“Quer que eu envie a cobrança?”

Isso deixa de parecer uma automação de WhatsApp. **Começa a parecer uma representação computacional da vida da pessoa.**

E essa descoberta ajuda muito a dimensionar nosso muro.

Nossa análise daqui para frente poderia funcionar assim: pegamos **uma persona por vez**, fazemos praticamente um dia inteiro da vida dela — manhã, trabalho, imprevistos, dinheiro, mensagens, deslocamento, compromissos — e vamos marcando cada necessidade como:

**já coberta por tijolo existente → combinação nova de tijolos → tijolo novo → pintura futura.**

Quando repetirmos isso para professor, personal, estudante, idoso, pai/mãe, contador, criador etc., começaremos a ver um fenômeno muito bom: no início aparecem vários tijolos novos; depois as personas novas começam a ser atendidas quase somente recombinando aquilo que já existe.

**Esse momento é um sinal de que a fundação está ficando suficientemente geral.**

E uma frase nova começa a surgir junto daquela primeira:

> **A Andy não precisa conhecer todas as profissões. Precisa compreender as estruturas que se repetem na vida das pessoas.**

Aí o muro deixa de ser dimensionado no chute. A gente começa a descobrir **experimentalmente qual altura ele precisa ter**.

---

## 9 — USER

Antes de eu adicionar qualquer nova informação, eu vou te fazer uma pergunta. Esse contexto dessa conversa já tá salvo na tua memória permanente? Se não, vamos tratar de salvar.

---

## 10 — ASSISTANT

Está **recuperável no meu contexto de longo prazo agora**, inclusive os pontos principais desta conversa — visão da Andy, contextualização por ação e não por público, modelo pessoal por usuário, Learning Plane separado do Execution Plane, integração-first, “tijolos antes da pintura” e o uso de personas para estressar a arquitetura.

Mas eu não quero te dar uma falsa garantia: **eu não tenho um comando manual para “gravar permanentemente” tudo isso na memória do ChatGPT**. A consolidação de memória acontece de forma assíncrona. Então, para um projeto desse tamanho, eu não trataria memória como fonte única de autoridade.

A forma segura é: **memória para continuidade de conversa + um checkpoint canônico no GitHub para não perder nada**. Esse checkpoint pode reunir exatamente o que estamos construindo agora: frases-mãe, princípios de produto, hierarquia Visão/Parede/Tijolo/Pintura, Contextual Disclosure, modelo pessoal, Learning Plane, capabilities/integrations, Mapa de Contextos Humanos e as decisões arquiteturais que forem surgindo.

Ou seja: **o contexto já está comigo, mas eu recomendo fortemente transformar esta conversa em um artefato permanente do projeto**. Aí não dependemos de memória nenhuma para reconstruir o raciocínio meses depois.
