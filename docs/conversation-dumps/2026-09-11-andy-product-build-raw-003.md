# Raw conversation checkpoint — Andy product/build

> Preservação bruta do bloco seguinte de 10 mensagens relevantes após o checkpoint raw-002. Sem síntese, consolidação ou decisão arquitetural adicional.

## 1 — ASSISTANT

**Sim. Aqui apareceu uma fileira nova de verdade no muro.** E ela é importante.

Eu chamaria essa fileira, tecnicamente, de **Artifact Plane** — e, pensando no produto, ela poderia aparecer para o usuário como algo do tipo **Arquivo da Andy** ou **Knowledge Vault**.

O ponto central é este:

> **Uma memória não é um arquivo. Um arquivo é uma fonte de verdade da qual memórias e conhecimento podem ser derivados.**

Então eu não jogaria uma planilha, PDF ou foto diretamente dentro do mesmo mecanismo de memória pessoal. Eu criaria uma camada própria para **artefatos**.

### No teu exemplo

Um funcionário manda uma planilha hoje pelo WhatsApp.

Amanhã manda outra pelo e-mail.

Para a Andy, depois da entrada, **WhatsApp e e-mail deixam de importar**.

Os dois canais viram apenas origens:

**WhatsApp → Ingestion Adapter**  
**E-mail → Ingestion Adapter**

Depois disso ambos entram no mesmo fluxo:

**arquivo recebido → identificação → armazenamento → extração → classificação → indexação → relacionamento → disponibilização**

Então aquela planilha teria algo conceitualmente assim:

**Artifact:** Relatório operacional agosto.xlsx  
**Tenant:** sua empresa/seu espaço  
**Recebido de:** funcionário X  
**Origem:** WhatsApp  
**Recebido em:** 10/09 às 15:32  
**Tipo:** spreadsheet  
**Hash:** abc123...  
**Conteúdo extraído:** abas, colunas, valores etc.  
**Embeddings:** derivados do conteúdo relevante  
**Relacionamentos:** filial 235, funcionário X, relatório mensal

No outro dia chega outra pelo Gmail. Ela entra exatamente na mesma estrutura.

### Onde o arquivo físico fica

Eu **não colocaria o XLSX, PDF ou foto como BLOB dentro do PostgreSQL**.

O arquivo original ficaria em **Object Storage**, algo S3-compatible.

O PostgreSQL armazenaria o cérebro sobre aquele arquivo:

identidade, `tenant_id`, hash, origem, remetente, data, tipo, localização física, permissões, informações extraídas, relacionamentos e status de processamento.

Isso escala muito melhor.

---

### E tem uma coisa elegante no teu exemplo da planilha duplicada

Se a mesma planilha chegar pelo WhatsApp e pelo e-mail, podemos calcular um **hash do conteúdo**.

Se for exatamente o mesmo arquivo:

**1 Artifact**  
**2 receipts/origens**

Ou seja, não duplicamos o arquivo, mas preservamos:

“Recebi pelo WhatsApp em tal data.”

“Recebi novamente por e-mail em tal data.”

Isso se chama **proveniência** ou **lineage**.

E vai ser absurdamente útil.

---

## A fotografia do comprovante é ainda mais interessante

Você pergunta:

> “Andy, quando eu recebi aquele comprovante de energia da filial 235?”

Aqui entram **três mecanismos diferentes**.

A visão/OCR lê a fotografia e encontra coisas como:

“energia elétrica”  
“filial 235”  
“pagamento”  
talvez valor, vencimento, fornecedor etc.

O embedding ajuda a Andy a encontrar aquela imagem quando você não lembra o nome do arquivo.

Mas atenção: **a resposta “quando eu recebi?” não deveria vir do embedding nem do OCR.**

Ela vem da informação canônica:

`received_at`

O embedding responde:

**“qual documento o Leonardo provavelmente está procurando?”**

O metadata responde:

**“quando ele chegou?”**

Essa separação é importantíssima porque evita usar IA para responder aquilo que o banco sabe com precisão.

---

### E planilhas exigem mais do que embedding

Isso também é um tijolo importante.

Imagine que seu gestor pergunte:

> “Qual foi o total produzido pelas 10 equipes no mês passado?”

Não quero que a Andy tente responder isso fazendo similaridade vetorial sobre a planilha.

Ela precisa **ler estruturalmente o XLSX**:

abas  
linhas  
colunas  
datas  
números  
fórmulas  
cabeçalhos

e transformar aquilo numa representação pesquisável.

Então teremos dois tipos de busca trabalhando juntos:

**busca semântica** para descobrir *qual informação/documento é relevante*;

**consulta estruturada** para responder números, datas e dados exatos.

Isso aumenta bastante o poder do sistema.

---

## E o acesso do João?

Também apareceu outro tijolo novo aqui: **Access Grants**.

Quando você disser:

> “Andy, libera esses relatórios para o João.”

Ela não deveria simplesmente expor seu armazenamento.

Ela cria uma autorização específica.

O link aleatório seria, tecnicamente, algo como um **capability token de alta entropia**.

Ele poderia dizer:

**pode acessar somente estes documentos**,  
**somente leitura**,  
**expira em 7 dias**,  
**pode ser revogado**,  
**acessos ficam auditados**.

E esse compartilhamento pode ser de:

um arquivo,  
uma coleção,  
um relatório,  
ou até uma **visão filtrada** dos dados.

Isso último é forte.

Talvez João nem precise baixar as 50 planilhas.

Ele entra no link e pergunta:

> “Me mostra somente os dados da filial 235.”

A Andy responde sobre **o conjunto que você autorizou**.

---

## Então qual fileira exatamente surgiu?

Eu desenharia assim:

**Artifact / Knowledge Plane**

E dentro dessa fileira existem inicialmente estes tijolos:

1. **Artifact Registry** — identidade de cada arquivo.
2. **Artifact Storage** — armazenamento físico dos originais.
3. **Source Receipt / Provenance** — de onde veio, quem enviou, quando chegou.
4. **Extraction Pipeline** — PDF, XLSX, DOCX, imagens, áudio etc.
5. **Structured Extraction** — números, tabelas, entidades e campos.
6. **Semantic Index** — embeddings.
7. **Artifact Relationships** — filial, funcionário, período, projeto etc.
8. **Access Grants** — quem pode acessar o quê e por quanto tempo.
9. **Audit Trail** — quem recebeu, abriu, compartilhou, revogou.

Essa é uma fileira nova porque ela serve para **todos os tipos de usuários**, não apenas para esse gestor.

Professor recebe provas e documentos.

Contador recebe notas e PDFs.

Personal trainer recebe avaliações e fotos.

Família guarda documentos.

Empresa recebe relatórios.

Estudante recebe apostilas.

De repente um exemplo específico revelou uma **primitiva universal**.

---

E olhando para o Attention Router atual, nós **não estamos partindo de zero**, mas também não temos essa camada pronta. Hoje já existe `MediaArtifactRow`, porém ele está modelado especificamente em torno de mídia de entrada/saída, com finalidades como `VOICE_INPUT` e `TTS_OUTPUT`; não é ainda um repositório genérico de documentos, planilhas e imagens pesquisáveis. Também já temos o arquivo/memória de conversas. Portanto, eu não tentaria esticar essas estruturas até virarem outra coisa: criaria um domínio genérico de `Artifact` e faria memória e conversas **apontarem para ele quando necessário**.

E tem uma frase que eu guardaria junto das outras:

> **A Andy não deve apenas lembrar o que sabe. Ela deve conseguir encontrar a evidência de onde aquilo veio.**

Isso muda muito o tamanho do muro.

Porque agora a Andy não é apenas memória + decisão.

Ela começa a possuir **memória, conhecimento, documentos, evidências e autoridade sobre o compartilhamento dessas evidências**.

E como completamos outro bloco de 10 mensagens, eu também já preservei esse segundo lote bruto no mesmo PR #33, em `docs/conversation-dumps/2026-09-10-andy-product-brainstorm-raw-002.md`, sem fazer destilação nem organização.

## 2 — USER

Vamos construir

## 3 — ASSISTANT

Vamos. E eu já comecei **pelo primeiro tijolo real do Artifact Plane**, sem pular para OCR, embedding ou interface.

Criei a branch `feat/artifact-registry-v0` e abri o **PR #34 — `feat: add tenant-scoped Artifact Plane V0`**. Ele está isolado da `main`, como combinamos.

O que já foi construído nesse primeiro corte:

- **`Artifact`** passa a representar o arquivo canônico dentro de um tenant. A identidade é baseada em `tenant_id + SHA-256`.
- **`ArtifactReceipt`** representa cada vez que aquele arquivo chegou. Portanto, a mesma planilha recebida pelo WhatsApp hoje e pelo e-mail amanhã pode resultar em **1 artifact + 2 receipts**.
- Cada receipt preserva **origem, conta de origem, identificador externo, remetente e `received_at`**.
- A deduplicação acontece **somente dentro do tenant**. Dois clientes diferentes que tenham o mesmo arquivo continuam tendo universos independentes.
- O mesmo evento recebido duas vezes é idempotente; se o mesmo identificador de origem reaparecer apontando para outro conteúdo, falha fechado.
- O arquivo físico **não vai para o PostgreSQL**. O banco guarda uma referência opaca ao storage. Esse é justamente o encaixe para o próximo tijolo, o Object Storage.
- Cada artifact também ganha um **`ResourceRow`**, aproveitando a ontologia que já existia no Attention Router. Isso é importante porque depois conseguimos relacionar o documento a funcionário, filial, projeto, período, capabilities, grants etc., sem inventar um segundo universo de entidades. O projeto já possuía `Resource`, `Relationship`, `Fact`, `Timeline`, capabilities e grants tenant-scoped, então estamos encaixando a parede nova na estrutura existente.
- Criei a migration **`0036_artifact_registry_v0`** e testes sintéticos justamente para os cenários WhatsApp + e-mail, replay, conflito e isolamento entre tenants.
- Também deixei documentado explicitamente que **OCR, visão, XLSX/PDF, embeddings, links de compartilhamento e UI ainda não pertencem a este PR**. Eles vêm por cima da identidade e proveniência, não antes.

A primeira passagem do CI encontrou **uma única regressão mecânica**: havia um teste antigo afirmando que a migration final obrigatoriamente deveria ser `0035`. Como acabamos de criar a `0036`, ele naturalmente quebrou. Eram **1046 testes passando e 1 falhando**. Corrigi o contrato para reconhecer a nova cabeça da cadeia e acrescentei verificações estruturais das duas novas tabelas.

O head atual é:

`29aea550ef3bb87af8658621480b2649f1ef0577`

O **CodeQL já está verde** nesse head. O segundo Public CI ainda está terminando neste momento; secret scan e transport já passaram, Docker build passou, e PostgreSQL/Python estão concluindo. **Não vou fazer merge enquanto essa rodada não estiver toda verde.**

Mais importante que o código: acabamos de definir tecnicamente algo que ontem era só conversa:

**WhatsApp e e-mail deixam de ser “lugares onde estão arquivos”. Eles passam a ser apenas origens de evidência de um mesmo universo pessoal da Andy.**

O próximo tijolo, depois que este PR fechar verde, é bastante claro:

**Object Storage + Ingestion Seam.**

Aí deixamos de apenas *representar* um artifact e passamos a conseguir **receber bytes reais, armazená-los de maneira durável, calcular seu hash e registrar automaticamente o Artifact + Receipt**.

Depois vem extração. Depois indexação semântica. Depois autorização de compartilhamento.

Parede por parede.

## 4 — USER

Acho que já temos material para construir

## 5 — ASSISTANT

Sim. **Já passamos do ponto em que continuar só conversando traz mais valor do que começar a erguer a estrutura.** A visão ainda vai continuar crescendo, mas já temos informação suficiente para saber que não estamos construindo um “bot de WhatsApp”; estamos construindo uma plataforma pessoal contextual, multiusuário, com memória, autoridade, artifacts, aprendizado e integrações.

Eu mudaria nosso modo de trabalho agora: **ideia nova continua entrando, mas não interrompe a obra**. Só muda a fundação se revelar uma necessidade estrutural que realmente não estava prevista.

A ordem que está aparecendo para mim é esta:

1. **Identidade e isolamento do usuário** — tenant explícito de ponta a ponta, eliminando dependência perigosa de tenant padrão. Esse é o terreno da casa.
2. **Personal Context** — fatos, relações, preferências, informações pessoais, proveniência, confiança e histórico. É a representação persistente de “quem é o dono desta Andy”.
3. **Contextual Decision + Disclosure** — entender *o que está sendo pedido* e decidir `RESPONDER / COMPARTILHAR / PEDIR APROVAÇÃO / NEGAR`, independentemente de simplesmente classificar a pessoa como pai, amigo, desconhecido etc.
4. **Artifact Plane** — documentos, planilhas, fotos, comprovantes, arquivos vindos de qualquer canal; identidade, proveniência, deduplicação, storage, extração e depois busca semântica. Esse tijolo **já está sendo construído agora no PR #34**.
5. **Learning Plane** — observar escolhas, correções, aprovações e comportamento; gerar candidatos a aprendizado; consolidar personalidade e regras pessoais sem permitir que o aprendizado contorne autoridade.
6. **Capability/Integration Plane** — Google Calendar, Gmail, localização, mapas, outros serviços. A Andy não precisa possuir essas coisas; ela precisa saber acioná-las através de contratos comuns.
7. **Experience Layer** — aplicativo, aprovação bonita, onboarding, visual, voz, apresentação da memória. **Pintura.** Importante, mas deliberadamente depois.

E aconteceu uma coisa boa enquanto estávamos conversando: o PR #34 passou pelo CI e revelou somente **contratos antigos de testes que ainda consideravam `0035` como o último migration head**. Não era defeito do Artifact Plane. Corrigi também os dois testes PostgreSQL restantes que tinham essa expectativa fixa; agora o head do PR é `b07be318...` e uma nova rodada completa de CI já está em execução. O PR continua aberto e não será mergeado antes de ficar inteiro verde.

Também acho que precisamos mudar um pouco nossa unidade de construção. Em vez de:

**“vamos implementar a funcionalidade de compartilhar Pix”**

pensamos:

**Contexto → informação solicitada → objeto conhecido → autoridade de divulgação → execução**

Aí Pix vira somente uma utilização dessa parede.

Da mesma forma, em vez de:

**“vamos implementar o comprovante da filial 235”**

construímos:

**Artifact → Receipt → Provenance → Extraction → Retrieval → Access Grant**

e comprovante, relatório, foto, contrato, nota fiscal e planilha passam a ser aplicações do mesmo conjunto de tijolos.

Esse é o indicador que eu usaria daqui para frente: **se estamos construindo algo que serve só para um exemplo, provavelmente estamos construindo cedo demais no nível errado. Se o tijolo resolve dezenas dos exemplos que levantamos, provavelmente encontramos uma primitiva do produto.**

E tem um ponto que eu colocaria como **nossa próxima fronteira estratégica depois do Artifact V0**: antes de sair instalando OCR, embeddings e Google, eu quero fechar o **Personal Context + tenant explícito**. Porque todos os outros sistemas vão produzir informação, e nós precisamos primeiro garantir **de quem é essa informação, onde ela vive, de onde veio e sob que autoridade pode ser utilizada**.

A partir daqui, acho que dá para parar de falar em “ideia da Andy” e começar a falar em **arquitetura do produto Andy**.

Continuamos tendo ideias — só que agora cada ideia nova vai encontrar um lugar na construção em vez de criar uma construção nova.

## 6 — USER

Pronto

## 7 — ASSISTANT

Perfeito. **Então essa linha fica fechada como direção arquitetural.**

Daqui pra frente, cada novo cenário que você trouxer eu vou tratar assim: identificar a necessidade real, verificar se já cabe nos tijolos existentes, apontar quando exige tijolo novo e separar claramente o que é fundação do que é pintura.

E seguimos sem travar a conversa para organizar tudo agora. O material vai sendo acumulado, preservado e transformado em arquitetura conforme os padrões forem aparecendo.

Pode mandar o próximo cenário.

## 8 — USER

Já temos algo para construir?

## 9 — ASSISTANT

Sim. **Já temos obra de verdade para tocar agora**, e o próximo passo já está bem definido.

O primeiro tijolo, o **Artifact Plane V0**, está pronto para entrar: o PR #34 continua aberto e mergeável, e agora tanto **Public CI quanto CodeQL estão 100% verdes** no head `b07be318...`.

Depois dele, eu seguiria nesta ordem:

1. **Fechar o tenant explícito de ponta a ponta.** Eliminar dependência silenciosa de `DEFAULT_TENANT_ID` nos caminhos de produção. Cada evento precisa nascer sabendo de qual universo pertence.
2. **Construir o Personal Context V0.** Um domínio canônico para fatos, preferências, relações, informações pessoais, proveniência, confiança e autoridade de uso.
3. **Criar o Context Retrieval.** O motor pergunta: “para este tenant e esta situação, qual pequena parcela da vida dessa pessoa é relevante agora?” — sem carregar tudo.
4. **Voltar ao Artifact Plane e colocar Object Storage + Ingestion Seam.** Aí começamos efetivamente a receber bytes de WhatsApp, e-mail, aplicativo etc. e transformá-los em artifacts.
5. Depois entram **extração estruturada/OCR → embeddings → busca semântica → Access Grants**.

Ou seja, já temos uma sequência que não depende de inventar mais produto:

**Artifact identity/provenance → tenant forte → Personal Context → retrieval → storage/ingestion → compreensão dos artifacts.**

E eu considero **tenant explícito + Personal Context** a próxima fileira mais importante. Porque depois que começarmos a despejar documentos, fotos, agenda, localização e aprendizado dentro da Andy, o sistema já precisa saber, sem ambiguidade:

> **de quem é, de onde veio, o que significa e com que autoridade pode ser usado.**

Então sim: **não precisamos de mais brainstorming para continuar construindo.** O brainstorming agora serve para testar a parede enquanto a gente ergue.

## 10 — USER

Vamos
