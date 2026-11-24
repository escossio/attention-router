# Continuidade da observação contextual no MGMT

Esta composição sucede a árvore experimental `be9326a0c3d5fb750ff241d577e1686af8acb3fa` do SHA `a9a85dd2b8d55b2be066de95ebde586a71b026b5`. O patch validado foi recomposto primeiro, com árvore idêntica, sobre a unidade semântica após #461. O delta posterior fica no leitor de evidência, na apresentação e na verificação de OTLP. As dez abas existentes, CONTEXT / PERFORMANCE e os Run IDs anteriores permanecem acessíveis nas respectivas custódias.

## Dois sinais independentes

- **Eventos locais.** LIVE usa a origem monotônica das operações e a idade dos eventos. As bordas de aquisição, checkout e checkin permitem abrir TRACE enquanto a posse ainda está ativa. O caminho até o checkout inclui trabalho interno do pool. A fase é inferida da pilha de eventos; livres são estimadas a partir das posses observadas. O Tempo pode disponibilizar spans já encerrados enquanto os demais continuam ativos. A timeline local conserva evidência anterior à conclusão do trace.
- **Cobertura OTLP.** O auditor usa um snapshot de eventos locais e a API de consulta do Tempo existente. Registra Run ID, SHA, árvore, digest dos eventos, hora, traces e spans esperados, observados e erros de consulta. O rodapé da aba indica cobertura parcial, consulta indisponível, verificação do recorte, verificação final ou recorte desatualizado. Eventos novos invalidam o selo do recorte anterior. Ausência de span num recorte ativo pode refletir atraso de ingestão. Um teste final depois do flush distingue essa situação de uma lacuna persistente. Nenhum painel dispara consultas em série ao Tempo.
- **Comparação.** O COMPARE exibe as duas identidades e separa repetição na mesma árvore e modo, mudança de medição, mudança de código e mudança simultânea. Com worker, corpus, consultas, imagem, configuração ou condição diferentes, explicita a comparabilidade limitada. Variações no mesmo SHA entram na distribuição experimental. Para uma alteração de código, use repetições pareadas em ordem alternada, congele os parâmetros e registre o intervalo entre execuções. Um efeito pede sinal repetido acima da dispersão observada. O deadline do cliente censura parte da cauda. Classifique recusas por autoridade e abstenções separadamente da disponibilidade.

Auditoria pontual em diretório privado de execução, com o endpoint loopback de consulta existente.

```bash
python3 -B ops/observability/grafana/g7/audit_otlp.py "$run_dir" --tempo-url http://127.0.0.1:29420
```

O auditor consulta spans existentes, descarta atributos e grava `otlp-coverage.json` por rename atômico no diretório da execução. Vincule a execução completa com o manifesto e o digest dos eventos. O arquivo permanece fora da raiz estática. Em caso de timeout do transporte, conserve a análise parcial e corrija somente o acesso de laboratório. Um replay de carga exige motivação registrada. O ensaio já encerrado com 126/126 traces e 3.438/3.438 spans é a referência de cobertura, preservada em #459.

## Dependências de bancada e proposta de operação

O ensaio anterior encadeou duas direções separadas. O CI02 enviou para o loopback `19418`, encaminhado via AGT01 até o Tempo isolado no CI01. A consulta saiu de AGT01 em `29420`. A projeção em CI02 chegou ao navegador por outro SSH. O runner M1 e o reader do laboratório foram iniciados em sessões SSH. A réplica HTTP foi iniciada pelo executor interativo. O Tempo e Grafana isolados têm política Docker de reinício `no`. A interrupção do túnel anterior preservou eventos locais e produziu uma lacuna OTLP demonstrada. O Supervisor efetivo já é supervisionado pelo systemd com `Restart=on-failure`.

1. Usar a admissão e o lock atuais dos workers para executar o ensaio com a identidade do job e do Run ID persistidas antes do primeiro evento. Vincular stdout, saída e PID ao registro do ensaio no worker. Para execução interativa autorizada, usar o supervisor do sistema no próprio worker como lançamento transitório com estado persistido. Reconectar para ler o status. Um cliente que volta a conectar consulta o Run ID existente. Reinício de monitor acompanha o job vigente e preserva as saídas anteriores.
2. Usar o Collector, Tempo e Grafana já mantidos no ambiente isolado pelas rotas de observabilidade aprovadas. Quando não houver rota estável, tratar o transporte SSH como dependência isolada e usar a supervisão e health check do host para o encaminhamento já existente, com portas loopback fixadas. A supervisão do encaminhamento pode recuperar transporte. Um erro de transporte não deve reexecutar a carga. Registrar janela perdida, manter eventos locais e rodar a auditoria de cobertura após recuperação.
3. Reutilizar a saúde do Collector e do Tempo no Zabbix/Grafana. Separar a indisponibilidade da origem local da falta de cobertura do backend. A janela de publicação do MGMT deve conferir a compatibilidade e o acesso de operador ao ambiente isolado. O runtime privado segue OFF e com as guardas de topologia atuais. Uma rota de telemetria de produção depende de decisão própria, inclusive quanto ao isolamento do namespace.

## Decisões e reversão

A decisão de integração revisa o delta de observação sobre a unidade semântica efetivamente integrada. Conferir SHA, árvore, checks aplicáveis e hashes dos quatro arquivos efetivos. A decisão independente de publicação valida o pacote, a autenticação do Apache, as dez abas anteriores, CONTEXT / PERFORMANCE, saúde da origem e leitura restrita à projeção autorizada. Reutilizar o fileset e o procedimento de backup/reversão em [publicação do Supervisor](mgmt-pool-lifecycle-publication.md). A evidência de laboratório, a aceitação de desempenho G7 e a publicação produtiva têm estados próprios.

[Primeira lacuna e replay OTLP](https://github.com/escossio/attention-router/issues/459#issuecomment-7138202070)
