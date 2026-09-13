# Raw conversation checkpoint 002 — Andy product brainstorming

Preserved without consolidation. This file intentionally keeps the conversational material in sequence so it can be distilled later.

## Message 1 — Assistant

Sim — **dá para fazer direto pelo GitHub**, e eu já fiz agora.

Salvei **as 10 mensagens anteriores**, em ordem cronológica e **sem tentar organizar, sintetizar ou decidir arquitetura**. A ideia foi exatamente congelar o “material bruto” para a gente pensar depois sem risco de perder nuances.

Ficou assim:

`docs/conversation-dumps/2026-09-10-andy-product-brainstorm-raw-001.md`

Commit:

`9da2f7d8548d49a0d027df3866c38aa5c761b89a`

E, respeitando nossa governança de não escrever direto na `main`, abri o **PR #33** com um único arquivo e 416 linhas.

O modelo que eu sugiro manter daqui pra frente é simples: **a cada bloco de 10 mensagens relevantes, arquivamos bruto; organização, filtragem, hierarquia e transformação em especificação ficam para outra etapa.** Assim nosso “papo furado” vira matéria-prima preservada, sem a obrigação de entender tudo enquanto a ideia ainda está nascendo.

Isso também resolve uma coisa importante: podemos continuar conversando soltos, sem ficar interrompendo o pensamento para documentar bonito. Depois teremos vários `raw-001`, `raw-002`, `raw-003`... e, quando chegar a hora, fazemos um trabalho separado de **destilação**, sem tocar nos originais.

## Message 2 — User

Certo, agora vamos lá. Decidimos vender, o produto tá praticamente pronto. Estamos prestes de vender. Vamos vender pra um usuário. Compreendemos que nós vamos ter um aplicativo. O contexto daquele usuário de aprendizado ficará salvo de que forma e aonde. Eu creio que deveria ser criada uma tabela no banco de dados reservado para aquela pessoa e os dados e informações, até mesmo embeds criados daquelas informações de contexto. O que você acha? E a pergunta final, mais importante é: pra isso acontecer, o que nós precisamos mudar agora pra nossa evolução?

## Message 3 — Assistant

Sim. **A tua intuição está certa no conceito — cada usuário precisa ter um espaço lógico próprio de contexto, aprendizado e memória — mas eu não criaria uma tabela física nova para cada pessoa.** Isso viraria um inferno operacional conforme o produto crescesse.

Eu faria a Andy nascer como um sistema **multi-tenant de verdade**: cada cliente é um `tenant`, e todos os dados pessoais carregam `tenant_id`. O banco pode ser o mesmo, mas do ponto de vista lógico cada usuário vive dentro do seu próprio universo. O projeto já tem um começo disso: existe `TenantRow`, várias tabelas principais já carregam `tenant_id`, e `MemoryActorRow` também é explicitamente isolado por tenant.

E tem uma notícia melhor ainda: **nós já construímos sem perceber parte do tijolo que essa visão exige.** O módulo atual de memória foi feito como arquivo privado de conversas e memória de atores, com `MemoryCandidate`, `MemoryClaim`, `MemoryEvidence` e histórico de extração. E ele foi deliberadamente separado de decisão, review, execução e outbound. Isso encaixa quase perfeitamente naquele `Learning Plane` que acabamos de conceituar.

Então eu não criaria “um novo sistema de memória”. Eu **evoluiria o que já existe para virar o Personal Context da Andy**.

### Como eu armazenaria a Andy de cada usuário

Imagine que o cliente João instala a Andy.

Nasce algo como:

**Tenant João**  
↓  
**Owner João**  
↓  
**Personal Context João**

Dentro desse Personal Context existem famílias diferentes de conhecimento.

Uma coisa é um **fato**:

`professional.job = personal trainer`

Outra é uma **preferência**:

`communication.answer_length = short`

Outra é uma **informação disponibilizável**:

`payment.pix.primary = ...`

Outra é uma **regra de autoridade**:

`payment.pix.primary → AUTO_SHARE`

Outra é uma **relação**:

`Maria → filha`

Outra é aprendizado comportamental:

`prefere confirmar compromissos antes de criar na agenda`

E outra coisa completamente diferente é a evidência que fez a Andy acreditar nisso:

“Usuário informou diretamente.”  
“Usuário aprovou quatro vezes.”  
“Foi obtido do Google Calendar.”  
“Foi inferido de uma conversa.”

O que temos hoje em `MemoryClaimRow` já aponta para essa direção: há predicate, validade, status, staleness, supersessão, conflito e timestamps; e `MemoryEvidenceRow` liga o conhecimento à evidência e à confiança. Isso é uma base muito mais rica do que simplesmente guardar uma string de memória.

### E os embeddings?

**Sim, eu colocaria embeddings. Mas nunca trataria o embedding como memória.**

Essa separação é importante.

A informação verdadeira continua sendo estruturada:

`João prefere respostas curtas.`

O embedding é apenas um **índice semântico** que ajuda a localizar essa informação quando surgir algo como:

“Como ele gosta que eu responda?”

Então eu criaria algo conceitualmente como:

`memory_embeddings`

ligado ao objeto original:

`tenant_id`  
`source_type`  
`source_id`  
`embedding_model`  
`embedding_version`  
`content_hash`  
`vector`

Se a memória original mudar, invalidamos/regeneramos aquele vetor.

E para o estágio atual eu **não criaria outro banco vetorial externo**. Nosso PostgreSQL com `pgvector` seria perfeitamente suficiente para iniciar. Só separaríamos para outro serviço se escala futura justificasse.

Há uma regra de segurança importantíssima: **segredos não entram em embedding**. Senha, token, chave privada, OTP etc. jamais deveriam ir para vetorização. O módulo atual já possui lógica explícita para detectar e bloquear memória de credenciais/segredos, o que é uma excelente fundação para isso.

---

## Agora vem a tua pergunta principal: o que precisamos mudar **agora**?

Aqui eu vejo uma mudança arquitetural importante, mas não uma reconstrução.

**Hoje o Attention Router ainda pensa principalmente em mensagem → decisão → resposta.**

Para virar o produto que estamos descrevendo, ele precisa começar a pensar:

**Usuário → Contexto Pessoal → Evento atual → Memórias relevantes → Intenção → Autoridade → Decisão → Ação → Aprendizado.**

Esse é o próximo aumento real do muro.

E eu faria **cinco mudanças estruturais**, nessa ordem:

1. **Transformar `tenant_id` em identidade obrigatória do usuário no runtime.** Hoje ainda existem vários caminhos usando `DEFAULT_TENANT_ID`. Isso foi ótimo enquanto havia praticamente uma Andy, mas produto vendido para N pessoas exige que nenhuma operação de produção consiga “cair no tenant padrão” silenciosamente. O usuário deve ser resolvido na entrada e atravessar todo o fluxo explicitamente.

2. **Formalizar o `Personal Context` como domínio.** Não apenas memória de conversa. Ele passa a reunir fatos, preferências, relações, disclosure rules, informação armazenada, decisões aprendidas e suas evidências. Podemos reaproveitar fortemente as tabelas `Memory*` existentes em vez de duplicá-las.

3. **Criar uma interface de recuperação de contexto para o Decision Engine.** Algo como conceitualmente: “para este tenant, esta pessoa, esta conversa e esta intenção, me entregue somente o contexto pessoal relevante”. O motor não deve carregar a vida inteira do usuário em cada prompt.

4. **Adicionar a camada semântica/vectorial.** `pgvector`, embeddings tenant-scoped, regeneráveis e derivados da memória canônica. Isso é retrieval, não autoridade.

5. **Preservar o Learning Plane separado do Execution Plane.** O que aconteceu numa conversa produz evidências/candidatos de aprendizado **depois**. Aprender não deve modificar a decisão que já está sendo executada naquela mesma transação. E o código atual de memória já foi intencionalmente desacoplado da execução — eu manteria essa propriedade quase como lei arquitetural.

E eu acrescentaria desde a primeira migration dessa evolução duas coisas que parecem burocráticas, mas serão tijolos estruturais: **proveniência e exclusão**.

A Andy precisa saber de onde veio cada memória.

E quando um cliente disser **“apague tudo que você sabe sobre mim”**, precisamos conseguir apagar o tenant inteiro, seus vetores, conversas, fatos e derivados de maneira determinística.

Isso num produto pessoal não é luxo. É parte da fundação.

---

### O que eu **não** faria agora

Não faria Machine Learning.

Não faria interface de memória.

Não faria tela para editar personalidade.

Não criaria 30 categorias de fatos.

Não criaria banco separado por cliente.

Não sairia implementando Google Calendar ainda.

Essas coisas são pintura ou paredes que ainda dependem desta fundação.

O tijolo que precisamos colocar **agora**, olhando para onde queremos chegar, é:

> **“Existe uma Andy por usuário, e toda decisão dela é capaz de consultar um modelo pessoal isolado, rastreável e evolutivo daquele usuário.”**

Uma vez isso presente no Attention Router, quase tudo que estamos imaginando começa a encaixar: Pix, preferências, agenda, relações, estilo, aprendizado, Google, localização, personal trainer, professor, pai, contador.

E tem uma consequência que eu acho enorme: **nossa próxima feature talvez não deva ser outro comportamento da Andy. Pode ser tornar a Andy verdadeiramente multiusuário no nível do contexto pessoal.**

Porque aí deixamos de construir **a sua Andy** e começamos efetivamente a construir **o produto Andy**.

## Message 4 — User

Eu não sei ainda o que é tenant, acredita?

## Message 5 — Assistant

Acredito — e, na verdade, **entender isso agora vai clarear muita coisa do produto**.

**Tenant** significa literalmente algo próximo de **“inquilino”**. Em software SaaS, é o **universo isolado de um cliente dentro de uma mesma aplicação**.

Pensa no Attention Router inteiro como um prédio. Nós não construímos um prédio novo para cada cliente. Existe um prédio só: servidores, aplicação, PostgreSQL, workers etc. Mas cada cliente ganha seu **apartamento isolado** lá dentro. Esse apartamento é o **tenant**.

Então, quando João compra a Andy:

**Tenant João**

Quando Maria compra:

**Tenant Maria**

O banco continua sendo o mesmo. A tabela de memória também pode continuar sendo a mesma. Só que cada registro leva uma etiqueta invisível dizendo a quem pertence:

| tenant_id | informação |
|---|---|
| `joao` | prefere respostas curtas |
| `joao` | Pix X |
| `maria` | trabalha como professora |
| `maria` | endereço comercial Y |

Quando a Andy de João procura memória, o sistema obrigatoriamente pergunta:

**“Traga memória onde `tenant_id = João`.”**

Ela jamais vê a memória da Maria.

Então aquela tua ideia de **“reservar dados para cada pessoa” está certíssima**. Só que em vez de criar uma tabela física nova para cada cliente, usamos **as mesmas tabelas com isolamento por `tenant_id`**.

E tem uma diferença importante:

**Usuário não é necessariamente tenant.**

Usuário é uma pessoa que acessa o sistema.

Tenant é o espaço ao qual aquela pessoa pertence.

Hoje podemos vender para indivíduos e ter praticamente:

**1 usuário = 1 tenant.**

Mas imagine depois uma família:

**Tenant Família Silva**

Dentro dele:

pai  
mãe  
filho

Ou uma empresa:

**Tenant Academia XYZ**

Dentro dela:

dono  
recepcionista  
personal trainers

Por isso é inteligente não amarrar conceitualmente `user_id` e `tenant_id`, mesmo que inicialmente sejam quase 1:1.

### E isso toca diretamente no que precisamos mudar agora

Hoje nosso Attention Router ainda possui caminhos em que, se ninguém disser qual tenant está operando, ele pode usar um **`DEFAULT_TENANT_ID`**.

Durante desenvolvimento com praticamente uma única Andy, isso foi conveniente.

Num produto vendido, isso vira perigoso.

A arquitetura correta passa a ser:

**login/app/WhatsApp identifica o cliente → resolve `tenant_id` → esse tenant acompanha a mensagem inteira → contexto → memória → decisão → aprovação → execução → auditoria.**

O `tenant_id` seria quase como **a VLAN lógica da vida daquele cliente**. 😄

Pode existir todo mundo na mesma infraestrutura, mas **nenhum pacote deveria escapar para a VLAN do vizinho**.

E agora dá para reformular bem aquela frase anterior:

> **Cada cliente não precisa de um banco separado. Precisa de um universo de dados inequivocamente separado.**

É isso que multi-tenancy resolve.

## Message 6 — User

Então o tenant é algo técnico, mas funcional dentro do banco de dados Post, do Post. Eu não tô sabendo formular a minha pergunta. Eu quero saber se essa informação é algo técnico, é algo característico, é uma especificação direta dentro do banco, ou é uma analogia de organização.

## Message 7 — Assistant

É **um conceito arquitetural que vira algo técnico dentro do banco**. Não é uma funcionalidade nativa do PostgreSQL chamada “tenant”.

Ou seja: o PostgreSQL não tem um botão “criar tenant”. **Nós é que modelamos isso.**

No nosso caso, isso vira algo concreto assim:

`tenants` → uma tabela que representa os espaços/clientes.

E várias outras tabelas carregam:

`tenant_id`

Então uma linha de memória, uma interação, uma policy, um evento etc. pode dizer tecnicamente: **“isso pertence ao tenant X”**.

Portanto, o tenant é ao mesmo tempo:

**conceito de arquitetura** → “universo isolado de um cliente”

e

**implementação técnica** → tabela `tenants`, coluna `tenant_id`, foreign keys, filtros, regras de acesso e eventualmente RLS no PostgreSQL.

A analogia do apartamento/VLAN serve só para entender. A implementação é real e concreta no banco.

E tem outro detalhe importante: PostgreSQL oferece mecanismos que podem reforçar isso, como **Row Level Security**, schemas separados e até bancos separados. Mas nenhuma dessas coisas “é tenant” por si só. São formas de implementar isolamento de tenants.

No Attention Router de hoje, a abordagem já é principalmente:

**mesmas tabelas + `tenant_id` por registro.**

Então quando eu falo “tornar a Andy multi-tenant de verdade”, não estou inventando uma abstração. Estou dizendo: **garantir tecnicamente que todo dado e toda decisão estejam sempre vinculados ao tenant correto, sem possibilidade de cair no espaço de outro cliente.**

## Message 8 — User

Seguindo essa regra arquitetural, crescimento por usuário, qual o nível de escalabilidade que nós temos para isso? Por exemplo, digamos que nós temos 500 mil usuários, 500 mil usuários. Essa estrutura é escalável até que nível?

## Message 9 — Assistant

Sim — **500 mil usuários cabem perfeitamente nesse modelo arquitetural**. O número de tenants, por si só, quase não é o problema. Uma tabela `tenants` com 500 mil registros é pequena para PostgreSQL.

O que determina a escala real é outra coisa:

> **quantos dados cada usuário produz × quantos usuários estão ativos simultaneamente × quantas consultas fazemos por interação.**

Então existe uma diferença gigantesca entre:

**500 mil usuários cadastrados**, com 20 mil ativos por dia,

e

**500 mil usuários conversando simultaneamente com a Andy.**

O primeiro cenário é absolutamente plausível com PostgreSQL e uma arquitetura bem feita. O segundo já é uma infraestrutura distribuída de grande porte.

### O modelo `tenant_id` escala muito bem

A estrutura que estamos discutindo seria algo assim:

`memory_claims`

| tenant_id | dado |
|---|---|
| A | memória 1 |
| A | memória 2 |
| B | memória 1 |
| C | memória 1 |

Não fazemos uma tabela por usuário.

Então 500 mil clientes continuam usando, por exemplo, **uma única tabela `memory_claims`**.

O PostgreSQL encontra os registros através de índices como:

`tenant_id + created_at`

`tenant_id + predicate`

`tenant_id + actor_id`

E a consulta vira conceitualmente:

> “Procure somente dentro do universo deste tenant.”

Isso escala para **milhões, dezenas de milhões e centenas de milhões de registros** sem mudar o princípio arquitetural. A partir de volumes muito grandes, muda-se a infraestrutura por baixo — particionamento, réplicas, shards — mas **o conceito de tenant não precisa mudar**.

---

Agora olha como isso fica interessante para a Andy.

Imagine 500 mil usuários.

Se cada usuário tiver apenas:

**1.000 memórias/contextos persistidos**

já temos:

**500.000 × 1.000 = 500 milhões de registros.**

Isso é grande, mas ainda está dentro de um território perfeitamente possível para PostgreSQL bem projetado.

Se cada pessoa produzir:

**10.000 memórias**

viramos:

**5 bilhões de registros.**

Nesse ponto eu provavelmente já não gostaria que tudo estivesse numa única tabela física de uma única instância PostgreSQL.

Mas não precisaríamos redesenhar a Andy.

Faríamos algo como:

**Tenant → Shard Router → PostgreSQL cluster A/B/C/D...**

Por exemplo:

Tenant 1–100.000 → cluster A  
Tenant 100.001–200.000 → cluster B  
etc.

Para o motor da Andy continua existindo:

`tenant_id = XYZ`

Ela nem precisa saber fisicamente em qual servidor os dados estão.

Isso é exatamente o tipo de arquitetura que eu quero que preparemos **agora**: não construir servidores para cinco bilhões de registros hoje, mas evitar decisões que impossibilitem chegar lá.

---

### Onde eu vejo os primeiros limites reais

Curiosamente, **não acho que o PostgreSQL tradicional seja nosso primeiro problema**.

O primeiro problema provavelmente será o **context retrieval**.

Quando chega uma mensagem:

> “Lembra daquele cara que falei sobre a academia?”

A Andy não pode carregar 40 mil memórias daquele usuário e mandar tudo para o LLM.

Precisamos fazer:

**mensagem atual → busca semântica → 10–30 memórias relevantes → contexto do modelo**

É aí que entram embeddings.

E esses embeddings também obrigatoriamente carregariam `tenant_id`.

Então:

> “Procure semanticamente algo parecido com esta pergunta, mas SOMENTE dentro da memória deste usuário.”

Isso é extremamente importante.

Uma busca vetorial global sem isolamento correto seria um desastre: memória de João poderia aparecer na busca de Maria.

Por isso eu colocaria desde a primeira versão:

**tenant isolation antes da similaridade vetorial.**

---

### A segunda pressão será eventos, não memória

Uma Andy ativa produz muita coisa:

mensagem recebida → evento → decisão → auditoria → memória candidata → possível embedding → intent → outbox → resposta → confirmação etc.

Uma única conversa talvez gere 10, 20, 30 linhas em diferentes tabelas.

Então 500 mil usuários ativos podem facilmente gerar **centenas de milhões de eventos**.

E aí entram naturalmente:

**particionamento temporal**, por exemplo por mês;

**retenção**, porque nem toda telemetria precisa permanecer no banco quente para sempre;

**arquivamento**, levando histórico antigo para armazenamento barato;

**workers assíncronos**, para aprendizado, embeddings e tarefas demoradas não travarem a resposta;

**filas**, para distribuir trabalho horizontalmente.

Perceba que nenhuma dessas coisas muda a identidade da Andy.

São apenas mais caminhões entregando tijolos na mesma obra. 😄

---

## Eu dimensionaria nossa evolução em quatro níveis

**Nível 1 — Agora / primeiros milhares de usuários**

PostgreSQL único, `tenant_id`, índices corretos, worker, Redis/fila se necessário, pgvector.

Simples.

**Nível 2 — dezenas/centenas de milhares**

PostgreSQL mais forte, connection pooling, réplicas de leitura, particionamento das tabelas gigantes, múltiplos workers e serviços stateless.

Ainda uma arquitetura bastante convencional.

**Nível 3 — centenas de milhares/milhões**

Sharding por tenant, múltiplos clusters PostgreSQL, armazenamento frio para histórico, serviços de memória/context retrieval separados e filas distribuídas.

A aplicação continua enxergando `tenant_id`.

**Nível 4 — muitos milhões**

Aí podemos ter regiões geográficas, clusters independentes, roteamento de tenant, bancos especializados para eventos, vetores, objetos e talvez caches de contexto pessoal.

Mas o modelo lógico continua praticamente aquele que começamos hoje.

---

E existe uma decisão **muito importante para colocar nos tijolos agora**:

### Nunca depender de “um banco por usuário”

500 mil usuários significariam 500 mil databases. Isso seria operacionalmente horroroso.

Também não queremos:

500 mil schemas.

Nem:

500 mil tabelas de memória.

Queremos:

**poucas tabelas bem projetadas + tenant_id + isolamento forte.**

Depois, quando a escala exigir, distribuímos essas tabelas fisicamente.

---

E tem outro tijolo que eu adicionaria agora depois dessa tua pergunta: **RLS — Row Level Security do PostgreSQL**.

Hoje podemos garantir no código:

> “sempre coloque `WHERE tenant_id = X`.”

Mas seres humanos erram.

Com RLS, podemos fazer o próprio PostgreSQL dizer:

> “Esta conexão está operando como tenant X. Mesmo que o programador esqueça o `WHERE`, você não verá dados de Y.”

Para um produto como Andy, que guarda literalmente **a vida digital das pessoas**, eu considero isso um excelente segundo cinturão de segurança.

---

Então respondendo tua pergunta de forma bem objetiva:

**500 mil usuários não me assustam absolutamente nada nesse modelo.**

O que precisamos fazer hoje não é construir infraestrutura para 500 mil usuários.

Precisamos garantir que quatro propriedades estejam presentes desde a fundação:

**tenant explícito em todo dado; isolamento impossível de contornar acidentalmente; serviços stateless que possam ser replicados; e armazenamento que possa ser particionado/shardado depois sem mudar o domínio.**

Se fizermos isso, podemos começar com uma máquina pequena e, teoricamente, evoluir para milhões de usuários **sem reconstruir o cérebro da Andy**.

Essa última parte, para mim, é o verdadeiro teste da nossa fundação: **a máquina muda; a arquitetura mental do produto permanece.**

## Message 10 — User

Vamos lá pro próximo. Eu trabalho numa empresa. Eu faço a gestão de 10 funcionários. Esses funcionários precisam gerar, cada funcionário gera relatórios diferentes. Eles mandam esses relatórios pelo WhatsApp. O meu gestor é o João. O João frequentemente precisa dessas planilhas, desses dados. O meu trabalho é supervisionar essas planilhas. E o meu chefe quer ver o que ele quer ver. Essas planilhas vão chegando pra mim no WhatsApp ou por e-mail. Não vamos considerar distinção nisso. Eu quero que um dia, só pra exemplificar e contextualizar, uma pessoa me mande uma planilha pelo WhatsApp e no dia seguinte me mande uma planilha pelo e-mail. E automaticamente essas duas planilhas que vieram em origens diferentes estejam localizadas em um lugar só, reservado pela Andy, com capacidade de replicação por autorização pro meu gestor. Dando a ele um acesso aleatório, um link aleatório que ele vai acessar e buscar a informação que ele precisa. Esse ambiente. Já aproveitando o contexto, a capacidade de imagens. de um certo dia eu dizia assim: Andy, a respeito de um comprovante do pagamento de energia da filial 235, você consegue levantar pra mim a data que eu recebi isso? E a Andy conseguir trazer pelo fato de ter conseguido ler o texto da foto e ter salvo e criado embeds daquela foto. Precisamos subir uma fileira no nosso muro pra isso acontecer?
