# MARCO-2026-10-03-ANDY-COGNITIVE-ARCHITECTURE

**O coração da Andy — Arquitetura cognitiva pessoal persistente**

Data: 2026-10-03

Status: MARCO ARQUITETURAL

## Passagem do marco

> A Andy não precisa manter a vida inteira do usuário dentro do pensamento atual. Ela precisa ter observado essa vida, preservado as evidências, consolidado o que importa, relacionado pessoas, objetos, acontecimentos e tempo, aprendido padrões, formado expectativas e saber recuperar exatamente a parte certa desse mundo quando o presente exigir.

> A Andy não começa tentando automatizar a vida do usuário. Ela começa tentando compreendê-la. A automação aparece depois, como consequência da compreensão, da recorrência observada e da autoridade explicitamente conquistada.

## Tese

A Andy deixa de ser uma coleção de agentes, memórias e ferramentas e passa a ser uma arquitetura cognitiva pessoal persistente: uma representação viva, temporal, multimodal, auditável e governada do mundo particular de cada usuário.

## 1. A mudança de categoria

O ponto central é abandonar a ideia de que Andy é apenas uma assistente que recebe uma pergunta, consulta algumas informações e produz uma resposta. O objetivo é outra categoria de sistema: uma inteligência pessoal persistente capaz de formar progressivamente uma representação do mundo específico de cada usuário, atualizar essa representação ao longo do tempo, relacionar informações vindas de fontes diferentes, reconhecer padrões, perceber mudanças, detectar situações fora do esperado e usar tudo isso para construir contexto antes de raciocinar ou sugerir qualquer ação.

O diferencial não é simplesmente ter memória. Memória pode signific guardar que alguém escreveu determinada frase. Compreensão contextual operacional significa transformar acontecimentos dispersos em uma representação estruturada de pessoas, relações, objetos, propriedades, compromissos, negociações, hábitos, pagamentos, preferências, situações abertas, acontecimentos passados, expectativas futuras e exceções.

A ordem fundamental é: observar, compreender, estruturar contexto, reconhecer padrões, formar expectativas, perceber desvios, sugerir, solicitar autoridade e somente então eventualmente agir. A observação nunca pula diretamente para a execução.

## 2. Bootstrap de contexto pessoal

Uma nova usuária não deveria instalar Andy hoje e esperar meses até que a assistente comece a conhecê-la. Depois do primeiro login e das autorizações explícitas necessárias, deveria existir um Personal Context Bootstrap: um processo progressivo de construção do contexto inicial.

No aplicativo poderia existir a opção Construir meu contexto inicial. A primeira fonte poderia ser Importar conversas do WhatsApp ou Iniciar com texto do WhatsApp. O toque nesse botão não significa acesso irrestrito ao WhatsApp; significa que o usuário escolheu conscientemente usar determinado conteúdo como fonte de bootstrap.

O processamento não precisa bloquear o aplicativo. O backend cria uma sessão de bootstrap, recebe lotes autorizados, normaliza evidências e trabalha em segundo plano. O usuário pode fechar o aplicativo e o processo continua. Mais tarde a mesma arquitetura pode receber Gmail, calendário, documentos, fotos, contatos, localização, dados financeiros e novas integrações.

O bootstrap deve ser orientado por ganho informacional. Primeiro relações ativas, assuntos recentes, situações ainda abertas e padrões recorrentes; depois períodos mais antigos quando ainda houver valor marginal. Uma conversa que deixou de revelar contexto novo perde prioridade. Uma conversa que continua explicando pessoas, patrimônio, compromissos ou recorrências permanece relevante.

## 3. Mensagem não é conhecimento

Uma mensagem como Me manda o boleto desse mês é evidência, não é ainda o conhecimento final. O sistema precisa percorrer uma cadeia conceitual: mensagem, evidência, episódio, entidade ou relação, claim contextual, padrão e expectativa.

Quatro mensagens espalhadas por meses podem sustentar algo muito mais rico: determinada pessoa ocupa um imóvel pertencente à usuária, existe uma obrigação mensal associada a esse imóvel e essa pessoa costuma solicitar informações de pagamento. O texto original permanece preservado como origem e prova, mas a nova conversa não precisa reler todo o histórico.

A conversa altera o modelo do mundo; depois disso, o modelo do mundo passa a ajudar a compreender novas conversas. Esse é o salto de um arquivo de mensagens para uma memória semântica operacional.

## 4. O exemplo das doze casas

Se uma usuária possui doze casas alugadas, Andy precisa progressivamente compreender que existem doze propriedades diferentes, doze relações de ocupação, valores diferentes, vencimentos diferentes e históricos diferentes. Parte disso pode ser inferida a partir de conversas e posteriormente confirmada por fontes mais fortes.

Mensagens como manda o boleto, o aluguel ficou em mil trezentos e cinquenta, vence dia dez e já paguei podem sustentar a hipótese de que Ângelo ocupa a Casa 7, existe uma obrigação recorrente de aluguel, o valor esperado é aproximadamente mil trezentos e cinquenta reais e a data usual é o dia dez.

Mas obrigação esperada não é pagamento observado. Uma obrigação de outubro pode existir com valor, pagador e vencimento conhecidos e ainda estar em estado esperado. Uma transação confiável pode movê-la para paga; um valor menor pode torná-la parcial; a passagem do vencimento sem evidência suficiente deveria produzir algo como pagamento ainda não confirmado após o vencimento, e não automaticamente a afirmação categórica de que a pessoa não pagou.

A intensidade da conclusão precisa ser proporcional à qualidade da evidência. WhatsApp sozinho pode justificar Andy perguntar se a usuária sabe se o pagamento ocorreu. Uma fonte financeira autorizada e conciliada pode permitir uma afirmação muito mais forte.

## 5. Expectativa, exceção e surpresa útil

O salto cognitivo aparece quando Andy não sabe apenas o que aconteceu, mas também o que normalmente deveria acontecer. Se um aluguel costuma ser pago até o dia dez, o sistema pode representar um evento esperado.

No dia onze, se o evento esperado não foi observado, aparece uma diferença entre esperado e observado. Essa diferença pode produzir uma exceção contextual. Se onze de doze aluguéis já foram conciliados e um permanece sem confirmação, a saliência daquele único caso aumenta.

Assim nasce um lembrete que ninguém programou manualmente. Da mesma forma, uma frase casual como sexta-feira eu entrego o documento pode produzir um compromisso futuro. Se sexta chegar sem evidência de conclusão, Andy pode chamar atenção para a promessa. O lembrete emerge da compreensão do mundo e do tempo, não de um cron agendado por alguém.

## 6. Um modelo vivo, temporal e corrigível

O mundo muda. Uma casa pode estar à venda hoje e vendida meses depois; alguém pode morar num imóvel e depois sair; uma negociação pode começar, evoluir e terminar. Por isso os fatos precisam carregar origem, evidência, confiança, validade temporal e capacidade de supersessão.

Casa 3 está à venda e Casa 3 foi vendida não são necessariamente afirmações contraditórias; podem ser verdadeiras em intervalos diferentes. Esse é o território dos Temporal Knowledge Graphs: conhecimento cujo estado e relação evoluem no tempo.

Quando o usuário corrige Andy, a correção explícita deve prevalecer sobre inferências anteriores sem destruir a história. O sistema precisa saber o que acreditava, por que acreditava, quando mudou e qual evidência justificou a mudança.

## 7. Personal Knowledge Graph e Cognitive Graph

Um Personal Knowledge Graph representa entidades relevantes para um indivíduo, seus atributos e suas relações. É quase diretamente o universo que estamos descrevendo: pessoas, imóveis, veículos, empresas, documentos, contas, compromissos, negociações, pagamentos, lugares e acontecimentos.

Quando adicionamos tempo, temos algo próximo de um Personal Temporal Knowledge Graph. Quando usamos essa estrutura não apenas para armazenar relações, mas para recuperar contexto, formar expectativas, priorizar atenção e apoiar raciocínio, ela começa a funcionar como um Cognitive Graph.

No grafo, Nilvanda pode ser mãe de Leonardo e de Lívia. Nilvanda possui a Casa 1. Ângelo ocupa a Casa 1. A Casa 1 gera uma obrigação recorrente de aluguel. Cada relação carrega evidência, confiança, validade e provenance. Uma relação inferida não vira verdade silenciosamente; ela nasce como hipótese e pode ser fortalecida ou corrigida.

## 8. PostgreSQL e grafo têm papéis diferentes

PostgreSQL continua excelente para perguntas transacionais e precisas: qual pagamento foi registrado, quando foi criado, qual é o estado da obrigação e quais transações pertencem a determinado contrato. O grafo é excelente para perguntas relacionais: quem é Ângelo, o que o conecta a Nilvanda, qual imóvel está envolvido e por que uma mensagem sobre dinheiro pode ser relevante agora.

O grafo não precisa substituir PostgreSQL. Ele pode ser uma projeção cognitiva da verdade persistida. Inclusive o primeiro grafo pode morar no próprio PostgreSQL. A prioridade inicial é a semântica correta e a auditabilidade, não a escolha prematura de uma tecnologia de banco de grafos.

## 9. Entidades, eventos e episódios

O grafo não pode conter apenas entidades estáticas. Também precisamos de acontecimentos: conversas, pagamentos, decisões, promessas, compromissos, negociações e mudanças de estado.

Ângelo envia uma mensagem sobre pagamento. Essa mensagem está relacionada ao aluguel de uma casa. Nilvanda responde que ele pode pagar na sexta. A resposta produz um novo evento semântico: uma exceção de prazo concedida, ligada à obrigação e com uma nova data esperada.

Quando chegar sexta-feira, Andy não precisa reler toda a conversa para lembrar o acordo. O mundo já foi atualizado. Um episódio reúne eventos que pertencem à mesma situação e permite que a memória deixe de ser uma pilha de mensagens isoladas.

## 10. As camadas não podem se confundir

Raw Evidence não é Context Claim. Context Claim não é Pattern. Pattern não é Recommendation. Recommendation não é Authority. Authority não é Execution.

Uma mensagem pode ser evidência. Um conjunto de evidências pode sustentar um claim. Claims repetidos podem formar um padrão. Um padrão pode gerar uma recomendação. Uma recomendação aceita ainda precisa passar por autoridade antes de qualquer efeito externo.

Essa separação é quase religiosa na arquitetura da Andy porque impede que inferência, frequência ou capacidade técnica se transformem silenciosamente em permissão para agir.

## 11. O que um ser humano precisaria ter

Para desempenhar o papel que queremos, um ser humano precisaria distinguir identidade própria e identidade alheia, lembrar episódios específicos, consolidar conhecimento semântico, possuir conhecimento procedural, compreender relações sociais, proximidade e formalidade, representar tempo, objetivos, compromissos, preferências e rotinas, manter um modelo do mundo, perceber causalidade e conviver com incerteza.

Também precisaria saber de onde veio uma crença, escolher o que merece atenção, notar quando o observado diverge do esperado, planejar próximos passos, utilizar ferramentas, distinguir capacidade técnica de autorização e reconhecer quando ainda não há evidência suficiente.

Essa lista revela que estamos descrevendo algo mais próximo de uma máquina de estado cognitivo persistente do que de memória convencional de chatbot.

## 12. Arquiteturas cognitivas acadêmicas

A busca por uma arquitetura geral de inteligência é antiga. Soar separa memória de trabalho, memória episódica, memória semântica, conhecimento procedural e mecanismos de decisão. ACT-R também modela conhecimento declarativo, conhecimento procedural, percepção e ação.

Schank e Abelson, ainda nos anos 1970, trabalharam com scripts, plans and goals: estruturas que permitem reconhecer situações familiares sem reaprender tudo do zero. Para Andy, aluguel mensal, rotina de cobrança, compromisso futuro e negociação podem se tornar scripts ou esquemas reconhecíveis.

Na memória autobiográfica humana também existe distinção entre lembrar um acontecimento específico, possuir um conhecimento geral sobre a própria vida e manter representações mais abstratas de identidade. A cadeia episódio, conhecimento semântico e contexto pessoal tem forte paralelo com o que estamos construindo.

## 13. Generative Agents, MemGPT e contexto ativo

Generative Agents popularizou uma arquitetura de observação, memória, reflexão e planejamento: experiências são registradas, reflexões mais abstratas são produzidas e depois recuperadas para orientar comportamento futuro. A ideia é relevante porque mostra que memória útil precisa ser consolidada, não apenas armazenada.

MemGPT explora a analogia com memória virtual de sistemas operacionais: o modelo possui uma janela de contexto limitada, enquanto a memória de longo prazo pode ser muito maior. A solução não é colocar a vida inteira do usuário no prompt; é manter memória extensa e mover para o contexto ativo apenas o que é relevante.

Esse princípio aponta para um dos motores mais importantes da Andy: o Context Compiler. Ele recebe a situação atual, navega pelo mundo persistido e entrega ao modelo somente o subconjunto preciso de pessoas, fatos, eventos, padrões, exceções, preferências e limites de autoridade necessários para aquele momento.

## 14. Human Identity é continuidade; chat é episódio

A conversa não deve ser a unidade principal da identidade. Um novo chat não pode significar uma nova Andy. A unidade principal é a Human Identity e o mundo persistente associado a ela.

Uma conversa é apenas mais um episódio. Daqui a cinco anos, abrir uma nova tela deve continuar sendo uma interação com a mesma Andy que conhece as relações, o patrimônio, os projetos, os compromissos, as preferências e o histórico daquele usuário.

Isso cria um teste arquitetural poderoso: se trocarmos o modelo de linguagem e Andy esquecer a vida do usuário, construímos um chatbot. Se trocarmos o modelo e o mundo do usuário continuar intacto, construímos uma inteligência pessoal persistente.

## 15. GNN: o que é e o que não é

Graph Neural Network, ou GNN, não é um protocolo nem uma ontologia. É uma família de arquiteturas de aprendizado de máquina feita para dados organizados como grafos. Teoria dos Grafos define nós, arestas, caminhos e vizinhança. Knowledge Graph é uma forma de representar conhecimento. Graph Machine Learning é a área de aprendizado sobre grafos. GNN é uma família de modelos dentro dessa área.

Uma ideia central é message passing: cada nó recebe informação de seus vizinhos, agrega essas mensagens e atualiza sua representação. Depois de uma camada, o nó conhece melhor seus vizinhos diretos; depois de mais camadas, informações de regiões mais distantes começam a influenciá-lo.

GNNs podem produzir embeddings, classificar nós, prever relações candidatas, detectar anomalias e reconhecer estruturas semelhantes. R-GCN e Heterogeneous GNNs são especialmente interessantes quando existem muitos tipos de relações diferentes, como mãe de, possui, mora em, paga para e trabalha com.

## 16. Por que grafo desde o início, mas GNN não como fundação obrigatória

O grafo deve nascer cedo. Já a GNN pressupõe que o grafo existe, que seus nós e relações possuem semântica razoável, que existem atributos, exemplos, objetivos de treinamento e critérios de avaliação. Ela não responde como o mundo da Andy deve ser organizado; ela aprende sobre um mundo já representado.

No início de um usuário existe cold start: poucas entidades, poucas relações e poucos eventos. Uma GNN personalizada teria pouco material para aprender. Mais tarde, quando o grafo acumular anos de vida e milhares ou milhões de eventos, o cenário muda radicalmente.

Além disso, a explicabilidade importa. Se Andy acredita que Ângelo é inquilino, precisamos conseguir mostrar o caminho de evidências. Uma GNN pode sugerir uma relação com alta confiança, mas essa sugestão deve entrar como hipótese, nunca como verdade automática.

GNNs também têm limitações próprias, como over-smoothing e over-squashing. Portanto, GNN é uma poderosa camada de intuição estrutural, não um substituto para evidência, regras, causalidade, provenance ou autoridade.

## 17. Spreading activation e recuperação seletiva

A ideia de mapa neural também combina com ativação propagada. Uma mensagem envolvendo Ângelo e pagamento ativa inicialmente esses conceitos. A relevância se propaga para Casa 7, aluguel, obrigação de outubro e Nilvanda. Elementos distantes e não relacionados recebem ativação muito menor.

Esse mecanismo permite navegar por uma memória enorme sem recuperar tudo. A topologia do grafo ajuda o Context Compiler a escolher o que merece entrar no contexto ativo. Uma GNN pode no futuro aprender formas mais sofisticadas de produzir relevância, mas a recuperação seletiva não depende obrigatoriamente dela.

## 18. Trinta e cinco bocas são trinta e cinco sensores

WhatsApp, Gmail, calendário, GPS, banco, documentos, fotos, contatos, telefone, carro e casa inteligente não são apenas canais de entrada. São sensores diferentes observando partes diferentes do mesmo mundo.

WhatsApp observa conversas e relações. Gmail observa contratos, compras e notificações. Calendário observa intenção futura. GPS observa deslocamento. Banco observa execução financeira. Documentos observam fatos formais. Fotos observam pessoas, lugares e objetos.

Cada fonte possui uma visão parcial. O desafio central passa a ser reconciliar observações diferentes da mesma realidade. Isso se aproxima academicamente de Data Fusion e Information Fusion: combinar múltiplas fontes numa representação coerente do estado do mundo.

## 19. Conhecimento emergente por correlação

A informação mais poderosa pode nunca existir explicitamente em nenhuma fonte. WhatsApp sabe que Ângelo fala sobre aluguel. O banco sabe que uma transação esperada não apareceu. O calendário sabe que a data usual passou. O grafo sabe que Ângelo ocupa uma casa de Nilvanda. O histórico sabe que ele normalmente paga até o dia dez.

Nenhuma fonte isoladamente contém a frase existe uma anomalia no aluguel do Ângelo. Essa informação emerge da convergência de evidências. Essa é uma das formas pelas quais Andy pode surpreender de maneira útil: ela percebe uma situação que o usuário não formulou como pergunta.

Mas correlação exige disciplina. Fontes diferentes podem derivar da mesma origem; uma mensagem dizendo recebi o boleto por email e o próprio email não são necessariamente duas evidências independentes. Provenance, qualidade, independência, contradição e tempo precisam acompanhar cada inferência.

## 20. Graph Intelligence Layer

O Cognitive Graph pode receber uma camada de inteligência formada por ferramentas diferentes que não competem entre si. Regras explícitas resolvem inferências determinísticas. LLMs interpretam linguagem e ambiguidade. Embeddings ajudam na similaridade. Algoritmos de grafo percorrem caminhos. Spreading activation calcula relevância. GNNs aprendem padrões estruturais. Modelos temporais estudam evolução.

A Graph Intelligence Layer seria o conjunto dessas capacidades: graph traversal, regras semânticas, entity resolution, link prediction, similarity, anomaly detection, embeddings, GNN, modelos temporais e LLMs trabalhando sobre a mesma representação auditável.

O grafo continua explícito e verificável. As camadas probabilísticas propõem conexões, padrões e hipóteses; o sistema de evidência decide o que pode se consolidar.

## 21. Cognitive Loop: o algoritmo central

O coração da Andy começa a aparecer como um ciclo contínuo. Perceber. Identificar entidades. Relacionar ao mundo conhecido. Posicionar no tempo. Interpretar significado. Preservar evidência. Atualizar episódios. Consolidar conhecimento quando houver suporte suficiente. Atualizar o modelo do mundo. Detectar padrões. Formar expectativas. Comparar esperado com observado. Aumentar atenção quando houver desvio. Recuperar contexto. Raciocinar. Sugerir. Verificar autoridade. Executar se permitido. Observar o resultado. Aprender com o resultado.

Esse ciclo não termina. PDF, Excel, DOCX, MP3, calendário, WhatsApp e outros plugins tornam-se braços e ferramentas. A inteligência não está simplesmente em saber criar uma planilha; está em perceber que, dadas as situações atuais, uma planilha comparativa seria útil e então sugeri-la.

Uma opinião também deixa de ser uma pergunta genérica ao LLM. Ela pode ser uma avaliação contextual explicável baseada no estado atual, histórico relevante, relações, preferências, objetivos, padrões, consequências prováveis e grau de incerteza.

## 22. A arquitetura híbrida que emerge

PostgreSQL mantém estado transacional, integridade e evidências. O Personal Temporal Cognitive Knowledge Graph organiza entidades, relações e tempo. Regras explícitas tratam inferências determinísticas. LLMs interpretam linguagem e geram hipóteses. Embeddings e GNNs ajudam a reconhecer padrões estruturais. O Attention Router decide o que merece processamento. O Context Compiler monta o pequeno contexto ativo. O Authority Engine decide o que Andy pode fazer. Ferramentas executam ações.

A arquitetura inteira precisa preservar uma propriedade: conhecimento e autoridade são independentes. Andy pode conhecer profundamente uma situação e ainda não possuir permissão para agir sobre ela.

## 23. O valor do produto é continuidade cognitiva

A assinatura de Andy não financia apenas tokens de conversa. Ela financia armazenamento, indexação, processamento histórico, consolidação, grafo, embeddings quando necessários, reavaliação temporal, auditoria, background jobs e retenção de evidências.

O produto deveria ficar mais valioso quanto mais tempo o usuário o utiliza. Não porque possui mais mensagens, mas porque o modelo do mundo daquele usuário fica mais completo. O mapa cognitivo de cinco anos da vida de alguém é único e não deve desaparecer quando o modelo de linguagem, o telefone ou algum plugin for substituído.

A promessa de produto pode ser simples: Andy não começa de novo a cada conversa. Ela é a mesma inteligência pessoal ao longo do tempo.

## 24. A formulação do marco

O que está sendo construído não é apenas uma assistente com memória. É uma camada computacional persistente entre uma pessoa e seu mundo digital.

O nome técnico ainda pode evoluir, mas três conceitos ajudam a materializar a visão: Personal Temporal Cognitive Knowledge Graph para a representação viva do mundo; Graph Intelligence Layer para os mecanismos de inferência e aprendizagem sobre esse mundo; e Andy Cognitive Loop para o ciclo contínuo de percepção, consolidação, expectativa, atenção, raciocínio, autoridade, ação e aprendizagem.

A Andy não precisa lembrar de tudo no pensamento atual. Ela precisa ter vivido tudo, consolidado o que importa e saber recuperar o pedaço certo da própria história quando o presente exigir.

## Referências acadêmicas e técnicas

- [Soar Cognitive Architecture](https://soar.eecs.umich.edu/)
- [ACT-R Cognitive Architecture](https://act-r.psy.cmu.edu/)
- [Schank & Abelson — Scripts, Plans, Goals and Understanding](https://en.wikipedia.org/wiki/Scripts,_Plans,_Goals,_and_Understanding)
- [Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442)
- [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560)
- [Personal Knowledge Graphs: A Survey](https://arxiv.org/abs/2304.09572)
- [Temporal Knowledge Graph survey](https://arxiv.org/abs/2201.08236)
- [Neural Message Passing for Quantum Chemistry](https://arxiv.org/abs/1704.01212)
- [Modeling Relational Data with Graph Convolutional Networks](https://arxiv.org/abs/1703.06103)

## Regra de continuidade

GitHub é a fonte de verdade. Esta página e seus artefatos de publicação nascem versionados no repositório; o AGT apenas consome um SHA exato para gerar a narração derivada e publicar o conteúdo.
