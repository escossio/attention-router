# Andy Ops Live Supervisor · reconciliação e ciclo do pool

O Supervisor efetivo é servido pelo Apache em `mgmt-agt01.escossio.com` e executa em `/srv/projetos/andy-ops-panel`. A composição parte dos quatro arquivos efetivos abaixo e reaplica os pontos de extensão da aba experimental. O baseline versionado é a main `1a0626a679aecbb3c6d19498b8e48c4b742149eb` em `ops/provisioning/andy-ops-panel`.

| Arquivo sob o diretório do serviço | SHA-256 efetivo | SHA-256 versionado |
|---|---|---|
| `app.js` | `7e5964c7a113ac0b5f3f0e2f2d4c18bde96097afcfeabff93a9da950fde3c2c3` | `42d3d5ba3b53b75534ae6ec912e3c7265016ba090ea0abb3d209ef83a63b3a5e` |
| `index.html` | `75e00f7bd6ee7fdaf836666f65752d2d5e142ebc41b2a2b24ff2c1612afa5df8` | `05ce8e48b869e516718a5dba730f97289251b381640e58e59afd2f2a8c8ba9a3` |
| `server.py` | `2e1bb4e24cc86a6dc3409b4346bb068b23e77a22c4622bff9588dad89232ee67` | `d8f04c0c02640f918677c401db046ad6886500a76d5b30358bb940f60de88e78` |
| `styles.css` | `9769d36751ea5c8567d0f4d4c83a89b8001ff3515d3eafd9788e2c6eb510a2ca` | `f9977a9f48c44248635553203edc67ee96fce4850f656b5910dd365fa2bcf7e8` |

| Arquivo | Diferença e origem provável | Risco e composição |
|---|---|---|
| `app.js` | Histórico de despacho, tempos observados do worker, estados de iframe, links de domínios e Investigador. O mtime de 17 de novembro coincide com `server.py` e `styles.css`. Os backups locais incluem uma passada de domínios. | Manter os bytes efetivos e a seleção das abas. A aba nova carrega seu próprio JS e usa `state.lastPayload` de COMPUTE. |
| `index.html` | Quatro entradas locais adicionais, controles de DOMÍNIOS e dependência AR-1001. O mtime é de 18 de outubro e o arquivo tem backups locais de setembro e outubro. | Manter IDs de painéis, fontes, política de iframe e ordem relativa das dez abas. Inserir CONTEXT / PERFORMANCE e seu script após os pontos já validados. |
| `server.py` | Recepção do journal do Investigador, projeção AR-1001, navegação ROC, rotas proxy de mesmo domínio, atividade CI sem atribuição e histórico com timestamps de recibos. | Manter CSP, status, fontes e projeções existentes. Acrescentar os dois GETs contextualizados com upstream apenas em loopback e rota/identificadores permitidos. Verificar módulos locais que o servidor importa. |
| `styles.css` | Histórico de despacho responsivo e estilos do Investigador. O mtime de 17 de novembro coincide com a atualização do apresentador. | Manter as regras efetivas e apensar somente o CSS da aba nova. Validar telas pequenas e rolagem das tabelas. |

As datas e backups indicam intervenções locais, sem evidência suficiente para atribuir autor ou identificar um commit de origem. O pacote leva os diffs completos e os quatro módulos/ativos AR-1001 e Investigador requeridos pelo baseline efetivo. Os coletores e armazenamentos operacionais permanecem compartilhados. A aba contextual usa o reader de telemetria em loopback. O lançamento experimental e o diretório de evidência ficam fora da raiz estática do Supervisor.

## Medição de aquisição, posse e fase

- A fronteira `Pool.connect()` mede monotonicamente desde a solicitação até o checkout ou a exceção. O intervalo inclui estabelecimento e trabalho interno do pool quando aplicáveis. `checkout` e `checkin` delimitam as posses contextuais abertas. O painel estima livres como capacidade menos posses observadas. A tabela separada registra a amostra bruta de ocupação obtida dentro dos callbacks. A fase exibida é inferida da pilha de eventos do trace no momento da leitura.
- A posse inclui número e duração das operações no cursor, eventos de início, commit e rollback, timeout de aquisição e posse após desconexão. Tempo fora do cursor é o residual medido e inclui processamento local, transações e esperas que o driver não decompõe. Não há SQL, parâmetros, IDs de negócio ou conteúdo nos eventos. IDs efêmeros de posse e de trace são dimensões de investigação, sem labels agregados de alta cardinalidade.
- No M1 descartável, uma conexão observadora separada amostra apenas PID e categoria de espera de `pg_stat_activity` a cada 200 ms. O PID fica restrito ao processo. `Lock` observado é amostra, e SQLSTATE 55P03 indica um timeout confirmado. Duração contínua de lock não é dedutível dessas amostras. O aplicativo não consulta a tabela de atividade. Em instalações sem observador, o painel marca a fonte ausente.
- O controle e a execução instrumentada compartilham SHA, corpus, consultas, imagem, worker, afinidade e configuração. A nova ordem é controle seguido de instrumentado, inversa do par anterior. Medir custo incluindo coleta, exportação e lag da interface. As abstenções legítimas e os timeouts permanecem separados.

## Janela proposta do Supervisor

1. Fixar SHA do candidato, hashes do arquivo e de cada destino, conta operadora e janela. Conferir os hashes efetivos novamente e o diretório de trabalho da unidade. Preservar os arquivos de unidade, Apache e ambiente. Conferir as projeções existentes e validar o reader loopback no ambiente aprovado, com exposição autenticada.
2. Guardar em área privada 0700 o backup fresco dos arquivos do manifesto e do ambiente, metadados, PID e respostas mínimas de saúde. Verificar o tar, dependências e hashes. Manter os backups fora da raiz estática. Ensaiar a troca e o retorno sob outra raiz.
3. Sob autorização da janela, parar somente o Supervisor, aplicar o fileset, validar modos e sintaxe e configurar os dois valores contextuais aprovados. O pacote preserva Apache e os coletores. Iniciar a unidade e provar HTTP 401 sem sessão, caminhos com autenticação, fontes e navegação de onze abas. O teste de CONTEXT / PERFORMANCE deve incluir ausência de dados e um ensaio sintético com um trace selecionável.
4. Em caso de divergência de hash, regressão, falha de saúde ou de autenticação, parar somente o Supervisor. Restaurar arquivos e ambiente a partir do backup, retirar apenas os dois arquivos novos quando ausentes no baseline, conferir os hashes antigos e retomar a unidade. Confirmar as dez abas, autenticação e fontes. A retenção de evidências da bancada é independente.

O G7 permanece aberto para desempenho. O pacote prepara uma decisão independente de publicação. Nenhuma autorização de ativação deriva destes ensaios. [Trilha #459](https://github.com/escossio/attention-router/issues/459)
