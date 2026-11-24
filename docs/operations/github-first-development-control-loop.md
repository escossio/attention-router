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

## Promoção do contexto da Andy

O [gate #428](https://github.com/escossio/attention-router/issues/428) guarda a
decisão por operação. O registro é um comentário de aprovação do mantenedor
`escossio` na issue, em um bloco JSON. Campos obrigatórios incluem `version`
(1), `gate`, `operation`, `pr`, `sha`, `approval_ref` e `expires_at`
com fuso UTC, além de `artifact_sha256`. Use uma referência opaca para a autorização privada. Mantenha
catálogo, ACL, backup, identificadores produtivos e credenciais em guarda
privada.

O pré-voo confere a aprovação, uma revisão favorável no head final do PR, a
integração à main, os checks obrigatórios, as execuções distribuídas Python e PostgreSQL e o checkout limpo no SHA aprovado.
Os checks do commit resultante precisam ter sido executados sobre ele, mesmo
quando o PR foi homologado antes do merge. A decisão na issue registra a
evidência de execução dos workers ao lado dos checks obrigatórios.

`release_schema.py` e `release_roles.py` exigem `--github-comment-id` em
`apply` e `rollback`. O documento privado desses runners deve conter o mesmo
`approval_ref`. Use uma autorização própria por operação. O pré-voo é
executado antes da abertura da conexão PostgreSQL e se soma à verificação de
catálogo, backup e exclusão de execução concorrente. Use um checkout
dedicado, sem arquivos locais ou ignorados. Execute a partir da raiz com
`PYTHONPATH=. python -B` para carregar a aplicação local e preservar a árvore
limpa. Mantenha configuração e artefatos privados fora do checkout.

G5/G6 exigem `artifact_sha256` no registro e a imagem OCI identificada pelo
digest no pré-voo. O GitHub CLI precisa oferecer `gh attestation verify` para
validar a proveniência do workflow oficial no SHA aprovado. O entrypoint
do runtime deve chamar o verificador e consumir a mesma referência OCI sem
substituir o digest. Registre o resultado saneado na #428.


### Ensaio e janela G3/G4

1. Registre na #428 o SHA integrado e as evidências de revisão e CI desse SHA. Em G3/G4, `artifact_sha256` é o SHA-256 retornado por `source_digest(root)` do runner no checkout aprovado. Um rebase ou novo merge exige nova verificação.
2. Em janela autorizada, o operador usa o mecanismo institucional de backup e testa a restauração do mesmo arquivo em destino isolado. Confere a revisão, catálogo, ACL e integridade da restauração. Arquivo e prova ficam em guarda privada. O runner confere o hash do arquivo, o horário e o registro privado da restauração. A cópia e a restauração são anteriores à aprovação da janela.
3. Capture o catálogo e a ACL com `audit_live_readonly.py --output <nova-pasta-privada>` e a conexão de auditoria autorizada. G3 requer o catálogo de 0067. G4 requer nova captura em 0072, antes da instalação dos papéis. O rollback de cada gate usa o catálogo anterior à respectiva aplicação. `check` verifica em transação somente leitura. Guarde identificadores, ACL e material de backup fora do GitHub.
4. Drene os escritores na janela. Use o runner aprovado e `APP_ENV=production` com a conexão do alvo fornecida pelo mecanismo de credenciais. G3 usa `release_schema.py` e G4 usa `release_roles.py` com um arquivo de escopo privado contendo `tenant`, `owner`, `account` e a lista `indexer_roles` dos indexadores legados que receberão a projeção limitada. O rollback deve referenciar o mesmo escopo. Os limites por instrução são 1,5 s para lock e 20 s para execução. Contenção, drift, aprovação expirada ou veto da ferramenta encerram a janela e retornam à #428. O procedimento de recuperação usa esse mesmo canal autorizado.
5. G3 avança de 0067 a 0072 com a tabela de inscrição e os consentimentos vazios. G4 instala papéis sem senha e com RLS, e concede aos indexadores aprovados acesso à projeção e à função exclusiva do escritor. Conferir isolamento, leitura da conta correta e progresso real de um escritor legado antes de autorizar o próximo gate.
6. O caminho inverso requer drenar os processos, verificar a ausência de inscrições e consentimentos, reverter G4 e depois G3, com aprovações distintas. O retorno a 0067 preserva tabelas, índice e linhas de Contacts. Tombstones ou histórico de autoridade podem impedir downgrade. Nesse caso preserve os dados, encerre a transação e leve a decisão de recuperação ao proprietário com o backup já restaurado em ambiente isolado.

O formato privado dos campos de aprovação está em `verify_approval` de
`release_schema.py`. A confirmação e o campo `operation` identificam a
ação exata. A aprovação GitHub associa o mesmo `approval_ref` ao digest,
ao PR e ao prazo. A entrega de credenciais, inscrição de tenant e ativação
do canário exigem suas próprias decisões posteriores.

Na homologação descartável, o executor de CI deve criar e identificar seu próprio banco antes de selecionar a configuração de teste. O harness versionado faz essa associação apenas no contêiner efêmero.
