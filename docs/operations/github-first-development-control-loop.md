# Regra operacional de desenvolvimento: GitHub primeiro

Esta regra existe para preservar contexto, rastreabilidade e velocidade sustentável quando agentes, plugins e acesso remoto aceleram o ciclo de desenvolvimento.

## Princípio central

O GitHub é a fonte de verdade para **o que o sistema deve fazer**: contrato, implementação versionada, testes, histórico, PRs e checks.

O runtime é evidência para **o que o sistema está fazendo agora**.

Uma investigação de falha deve seguir esta ordem:

1. GitHub: localizar contrato, implementação, testes, commits/PRs e estado atual da `main`;
2. declarar o comportamento esperado com base nessas evidências;
3. só então consultar AGT/runtime/logs para observar o comportamento real;
4. comparar esperado versus observado;
5. corrigir a diferença mínima demonstrada;
6. validar, documentar e só depois avançar para outra frente.

Nunca usar o runtime como substituto da memória técnica do projeto.

## Antes de usar ferramentas

Uma informação nova do operador não dispara automaticamente uma investigação.

Antes de uma sequência de chamadas, o agente deve informar:

- objetivo da investigação;
- fonte que será consultada primeiro;
- escopo esperado;
- janela aproximada de duração;
- se a operação é somente leitura ou pode alterar estado.

Classes de duração:

- **curta**: até cerca de 2 minutos, poucas consultas somente leitura;
- **média**: cerca de 2 a 10 minutos, múltiplas consultas ou correlação entre repositórios;
- **longa**: acima de 10 minutos, builds, testes extensos, análise de logs ou investigação multi-camada.

Essas janelas são estimativas, não garantias. Se a operação ultrapassar claramente a janela anunciada, pare em um checkpoint útil, explique o que já foi provado e o que falta, e só então continue.

## Rajadas de ferramentas e contexto

Evite longas sequências silenciosas de chamadas.

Prefira rajadas pequenas e delimitadas, com checkpoint entre fases lógicas. O objetivo é impedir que velocidade de execução cause perda de contexto, duplicação de trabalho ou investigação na direção errada.

Ao surgir um segundo problema enquanto uma frente está aberta, registre-o e congele-o. Não misture frentes sem necessidade.

Antes de mudar de frente, registre:

- hipótese atual;
- evidência obtida;
- estado do Git/PR;
- próximo passo;
- pendências explicitamente congeladas.

## Regra para features aparentemente ausentes

Não conclua que uma funcionalidade ainda não existe apenas porque o comportamento observado falhou ou porque o contexto da conversa não contém sua implementação.

Antes de propor feature nova:

1. pesquise no GitHub o contrato e a implementação existentes;
2. procure testes que demonstrem comportamento já validado;
3. verifique histórico recente para identificar regressão ou mudança de contrato;
4. só então classifique o caso como feature ausente, regressão, divergência de versão ou falha de runtime.

Um comportamento anteriormente implementado e testado deve ser tratado primeiro como possível regressão ou divergência, não como greenfield.

## Uso do AGT e acesso remoto

O plugin do AGT é um acelerador e instrumento de observação, não a autoridade arquitetural.

Use-o para:

- confirmar versão e proveniência de artefato;
- observar logs e estado real;
- reproduzir falhas;
- executar testes autorizados;
- medir a diferença entre contrato e runtime.

Não comece pelo AGT quando a pergunta é "o que já foi implementado?" ou "qual é o comportamento esperado?". Essas respostas pertencem primeiro ao GitHub.

Não altere runtime, banco, sessão, tenant ou dados persistidos antes de capturar evidência suficiente e estabelecer o baseline versionado, salvo autorização explícita para reset de ambiente de desenvolvimento.

## Proveniência antes de depurar

Quando o comportamento depende de um binário, container ou APK, determine antes de aprofundar:

- repositório;
- branch;
- commit SHA;
- estado clean/dirty da árvore de trabalho;
- origem do artefato;
- build local ou CI;
- checks relevantes daquele SHA.

Se a proveniência não puder ser demonstrada e o ambiente ainda for de desenvolvimento, prefira gerar um artefato novo a partir de SHA conhecido e reproduzir o problema em baseline determinístico.

## Estado legado de desenvolvimento

Compatibilidade com dados temporários de desenvolvimento não é requisito automático.

Se tenant, sessão ou fixture antigos ficaram incompatíveis por evolução legítima de schema/contrato, é permitido recriar o estado após capturar evidência mínima. Isso não pode ser usado para esconder um bug reproduzível no fluxo atual.

O critério é simples: estado novo criado pela versão atual deve sobreviver ao ciclo normal que o contrato promete.

## Critério de velocidade

Velocidade útil é throughput de decisões corretas, não quantidade de comandos por minuto.

Quando houver conflito entre aceleração e rastreabilidade, reduza a velocidade instantânea para preservar:

- contexto;
- contrato;
- proveniência;
- evidência;
- reprodutibilidade;
- capacidade de retomada.

Uma sessão mais lenta que mantém o trilho é preferível a uma sessão rápida que exige reconstrução posterior.
