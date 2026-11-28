# Andy | Memória contextual incremental e substituível

**Registro de direção arquitetural — 28/11/2026**  
**Estado:** concepção aprovada como direção de trabalho; implementação e validação ainda não realizadas.  
**Base preservada:** `83b06b237f00ecbf08c2e2a1a92f9221ff641d3e`  
**Natureza desta branch:** frente documental e experimental independente. Não constitui autorização de promoção, migração ou ativação produtiva.

## 1. Por que esta frente existe

O propósito fundador da Andy permanece inalterado: inteligência contextual persistente, governada pelo proprietário, capaz de compreender, relacionar e atuar sobre seu mundo. O Attention Router fornece a infraestrutura de coordenação; a Andy representa a inteligência contextual.

O projeto acumulou uma infraestrutura substancial, contratos, testes, observabilidade e mecanismos de identidade e autoridade. Entretanto, a evolução recente da memória apresentou uma relação insatisfatória entre complexidade de implementação e utilidade demonstrada ao proprietário. A primeira consulta contextual real fundamentada ainda não foi homologada.

O problema não será tratado presumindo que todo o projeto está errado, nem presumindo que todas as escolhas técnicas anteriores precisam ser preservadas. A nova investigação deve identificar o menor ciclo cognitivo útil e as fronteiras que permitam evoluí-lo sem reconstruir a infraestrutura funcional.

As experiências anteriores continuam válidas como conhecimento adquirido, inclusive seus resultados negativos. O estado dos gates anteriores permanece inalterado: G3 NO_GO; canário real G6 pendente. A nova frente não constitui uma forma alternativa de executar operações interrompidas ou bloqueadas.

## 2. Princípio de engenharia: evolução sem substituição abrupta

A referência conceitual do proprietário é a implantação de roteamento estático seguida, quando necessário, da introdução de OSPF.

Uma rede pode começar com rotas estáticas, oferecendo comportamento simples e controlável. Um protocolo dinâmico pode posteriormente ser avaliado em coexistência com a configuração existente. A preferência administrativa permite transferir responsabilidade progressivamente, conforme o comportamento observado, sem exigir a eliminação prévia de todas as rotas estáticas.

A analogia é um princípio de engenharia, não uma equivalência literal entre roteamento e cognição.

Para a Andy, isso significa:

- Começar por um comportamento pequeno, útil, compreensível e demonstrável.
- Manter contratos estáveis entre consumidores e mecanismos cognitivos.
- Permitir que novos motores coexistam e sejam avaliados independentemente, com dados e critérios comparáveis.
- Transferir responsabilidades por capacidade, somente após validação e decisão explícita.
- Preservar a possibilidade de retornar ao comportamento anterior, sem perder informações nem exigir reconstrução.
- Nunca permitir que preferência de motor, pontuação ou confiança substitua autorização, isolamento, elegibilidade ou sustentação de evidência.

Uma lacuna funcional deve motivar uma extensão ou uma alternativa comparável, não a reconstrução obrigatória de toda a arquitetura.

## 3. Primeiro resultado que interessa ao proprietário

A primeira unidade de entrega não é um motor, um schema, uma ontologia ou uma quantidade de testes. É uma experiência cognitiva completa e observável, mesmo que pequena.

Exemplo exclusivamente sintético:

1. Registrar que Roberto possui um veículo azul e trabalha na empresa Alfa.
2. Encerrar uma interação e iniciar outra sessão no laboratório.
3. Perguntar, em linguagem natural, qual é a cor do veículo ou onde Roberto trabalha.
4. Recuperar a informação persistida e responder com fundamento identificável.
5. Corrigir a cor para branco.
6. Em consulta posterior, reconhecer branco como informação atual e preservar a evidência histórica da correção.
7. Diante de uma pergunta sem evidência suficiente, reconhecer a insuficiência, sem inventar uma resposta.

O laboratório deverá demonstrar persistência real entre sessões e reinícios apropriados, não apenas continuidade no contexto transitório de um modelo de linguagem.

Essa prova não equivale à autenticação, homologação ou autorização de uso de fontes reais da Andy. Nenhum desses resultados libera os gates anteriores.

## 4. Escopo conceitual e hipóteses de arquitetura

O núcleo inicial precisa apenas das responsabilidades necessárias para entregar o primeiro resultado:

- Registro de evidência e revisão.
- Armazenamento persistente.
- Recuperação de informações pertinentes.
- Interpretação e resposta fundamentada.
- Correção e abstenção.

A infraestrutura existente de comunicação, identidade, autoridade, execução, coordenação e observabilidade deverá ser preservada. Sua independência em relação à memória ainda precisa ser confirmada por análise dirigida de dependências. Não assumir que uma integração está demonstrada apenas por existir um ponto de extensão.

A inspeção anterior identificou `ClientContextReader`, `CompiledContextPacket` e `RankedEvidenceCandidate` como referências de contratos e abstrações existentes. Eles merecem avaliação de reaproveitamento; não constituem prova de uma interface universal de memória. Não copiar o runtime produtivo ou reexecutar sua trajetória bloqueada para viabilizar o laboratório.

A hipótese inicial é uma memória relacional pequena, com PostgreSQL dedicado e banco sintético independente, recuperação simples e interpretação por um modelo de linguagem substituível. Essa hipótese permite separar a adequação do banco da complexidade provocada pelo acoplamento com o ambiente operacional.

Não há decisão definitiva de banco, mecanismo de recuperação, modelo local ou comercial. pgvector, grafo, extração automática de entidades e motores especializados não são pré-requisitos da primeira prova. A ausência deles tampouco será tratada como restrição permanente.

Uma segunda arquitetura poderá ser avaliada em paralelo, inclusive com banco orientado a grafos, motor pronto de memória ou outro modelo de persistência. Ela deverá ser escolhida por capacidade, custo de integração e evidência, não pela obrigação de reproduzir os mecanismos internos da primeira.

## 5. Coexistência e integração

O objetivo de longo prazo é manter consumidores independentes das escolhas internas de cada motor de memória.

Primeiro, delimitar contratos mínimos de registro, recuperação, correção e resposta. Identidade e autoridade permanecem responsabilidades governadas fora da escolha do motor. Evidência, origem e revisão devem ser preservadas independentemente da tecnologia adotada.

Um mecanismo candidato poderá produzir resultados de comparação em ambiente sintético, sem assumir responsabilidade por respostas reais. A comparação deve usar os mesmos fatos, correções, perguntas, evidências e critérios de aceite; não exigir schemas internos idênticos.

A eventual preferência por um mecanismo será avaliada por capacidade, como recuperação direta, temporalidade ou relações, com comportamento de referência preservado. O desenvolvimento de um seletor genérico, roteador cognitivo ou sistema de promoção não é requisito da primeira entrega; esses mecanismos só deverão ser introduzidos quando a coexistência efetiva os justificar.

Não criar escrita duplicada em produção, sincronização de dados reais ou migração antecipada. Nenhum contêiner candidato terá acesso a fontes, credenciais, filas ou executores produtivos nesta etapa.

## 6. Laboratório: preparação em etapa própria

O proprietário indicou o AGT01 como ambiente pretendido para novos contêineres independentes.

A hipótese de laboratório contempla banco sintético persistente e serviço cognitivo separado, com possibilidade de uma segunda candidata posteriormente. Endereçamento e redes ainda não foram decididos. Foi mencionada a possibilidade de aproveitar uma sub-rede existente, inclusive a do PostgreSQL, mas isso não constitui decisão de conectar o laboratório ao banco ou aos serviços atuais. A separação de domínios e a ausência de acesso indevido precisam ser verificadas antes de qualquer escolha de rede.

O inventário inicial de recursos do host foi iniciado, mas a preparação foi interrompida pelo proprietário antes de criar infraestrutura. Retomar, em novo chat, apenas a avaliação necessária de capacidade, redes, imagens e nomes disponíveis. A opção de rede dedicada deve ser avaliada em função do isolamento e do custo, sem alterar regras, rotas, redes ou contêineres existentes.

A decisão sobre contêineres, volumes, limites de recursos e modelo de linguagem deverá ser apresentada de maneira concreta antes da execução. Não utilizar dados reais nem segredos do projeto como fixtures. Preservar todos os controles existentes; nenhum teste experimental autoriza operação produtiva.

## 7. Cadeia de evolução por capacidades

A sequência é uma direção de desenvolvimento, não uma lista de motores obrigatórios:

- **Lembrar:** persistir e recuperar uma informação simples entre sessões.
- **Compreender:** responder à mesma necessidade expressa de maneiras diferentes.
- **Corrigir:** preservar revisão e distinguir informação atual de informação superada.
- **Relacionar:** combinar evidências de diferentes acontecimentos.
- **Contextualizar:** tratar temporalidade, condições, conflitos e insuficiência.
- **Evoluir:** incorporar capacidades ou mecanismos melhores em coexistência, com retorno verificável.

Os critérios de segurança aplicáveis à capacidade em avaliação não são adiados. Capacidades futuras, por sua vez, não devem ser convertidas automaticamente em pré-requisitos da primeira experiência.

## 8. Tempo, complexidade e critério de decisão

O tempo de engenharia passa a ser uma variável explícita da arquitetura.

Antes de cada etapa, definir uma pergunta funcional, uma hipótese, um orçamento de esforço proporcional e a evidência de conclusão esperada. Avaliar:

- Tempo até o primeiro resultado observável.
- Esforço para acrescentar ou substituir uma capacidade.
- Tempo e esforço de experimentação com outro mecanismo.
- Facilidade de retorno, integridade dos dados e custo de manutenção.
- Precisão, cobertura, abstenção, latência e custo de execução.

Não prolongar uma investigação indefinidamente para preservar uma escolha anterior. Se uma hipótese falhar, registrar por quê, avaliar a menor alternativa e preservar o que já funciona.

A IA deverá atuar como arquiteta técnica: formular uma proposta concreta, justificar suas escolhas, apresentar consequências e alternativas relevantes, e assumir a responsabilidade pelo diagnóstico técnico. A decisão de finalidade e autoridade permanece com o proprietário. Não transferir a ele escolhas de tecnologia sem uma recomendação fundamentada.

## 9. Preservação e limites

Esta branch não modifica a main, não substitui a memória atual e não libera nenhum gate.

O estado anterior e as evidências históricas permanecem preservados. As passadas G3 e G6 não serão retomadas automaticamente por esta mudança de direção. Não repetir, contornar ou transferir para outra ferramenta operações previamente interrompidas. A frente experimental deve ser independente, não um caminho de promoção indireta.

Não alterar containers, redes, volumes, bancos ou serviços existentes; não coletar dados reais; não publicar código experimental, efetuar merge, release ou deploy sem autorização própria. O único commit autorizado neste marco é este registro documental na branch isolada.

## 10. Retomada em um novo chat

Ler este documento e os princípios fundadores. Reconciliar a main e os limites de atuação, sem reiniciar a sequência FUNC-011 nem reconstruir contratos existentes.

A primeira missão é propor a preparação mínima de um laboratório sintético independente no AGT01, com inventário dirigido de recursos, rede e persistência. Formular uma arquitetura concreta e justificar as escolhas, levando tempo de entrega e substituição futura em consideração.

Antes de criar infraestrutura, apresentar a delimitação: quais contêineres serão novos, quais responsabilidades terão, como permanecerão isolados e qual resultado funcional demonstrarão. Nenhuma escolha do laboratório deve alterar a infraestrutura atual ou conceder autoridade sobre fontes reais.

A referência principal de produto continua sendo [Andy: propósito e princípios fundadores](ANDY-PROPOSITO-E-PRINCIPIOS.md). Contexto histórico: [visão cognitiva](../architecture/andy-cognitive-architecture-vision.md), [contrato funcional #425](https://github.com/escossio/attention-router/issues/425), [governança operacional #428](https://github.com/escossio/attention-router/issues/428) e [continuidade #476](https://github.com/escossio/attention-router/issues/476).

**Princípio final:** fazer uma Andy pequena e útil, capaz de crescer sem destruir o que já funciona. A complexidade deve ser consequência de necessidades demonstradas, não condição antecipada para a primeira utilidade.
