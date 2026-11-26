# Preparação de ingresso independente (G3 / Janela A)

**PREPARATION ONLY.** Este recorte é uma proposta revisável de composição,
não um executor de release, um artefato de promoção, autorização de operação
ou prova de exclusão dos escritores legados. G3 permanece NO_GO.

## Origem e fronteira

A aplicação Andy já tem inicializador próprio na main. O inicializador
compartilhado no runtime legado encerra os demais servidores ao término
de qualquer listener e desabilita o lifespan ASGI da Andy. A separação
reutiliza os motores existentes, preservando autoridade, binding, audiência,
recibos e identidade de admissão. Não muda políticas, schemas ou o fluxo
funcional do MAESTRO.

O launcher operacional deriva de #374, no head identificado em
PROVENANCE.json. O delta é restrito à função de seleção do listener:
o valor de admissão do relay é entregue ao helper TLS original, em vez
de exigir admissão habilitada. O TLS continua obrigatório. A composição
preserva explicitamente o modo standby (TLS habilitado, relay desabilitado).
Nenhum flag produtivo deve ser alterado por este pacote.

**Dependência deliberada do legado:** os imports MAESTRO e o helper de
listener utilizados pelo launcher não estão presentes na aplicação da main.
O arquivo fica sob ops, como overlay operacional para a imagem legada exata,
e não sob attention_router/web da main. Não é um launcher para uma imagem
construída da main. Não integrar integralmente o draft #374 nem importar
sua cadeia de migrações. O ID de imagem local em PROVENANCE.json não é
digest de distribuição de um registry. A correspondência do artefato local
e a recuperação da imagem são precondições, não são verificadas pelo Compose.

Os três arquivos homologados são preservados byte a byte. Os quatro
módulos da Andy são extraídos do Git no SHA certificado registrado em
PROVENANCE.json e montados individualmente, somente leitura, nos destinos
dos manifestos. Não duplicar esses módulos nem copiar a árvore inteira
da main para o runtime legado. Conferir SHA256 de cada blob e identidade
da imagem antes de qualquer futuro uso. Uma alteração nos hashes exige
nova revisão do recorte afetado.

## Grafo e colocação

| Origem | Destino | Iniciador | Protocolo | Autoridade / dados |
| --- | --- | --- | --- | --- |
| Produtores Andy | Ingresso Andy | Produtor / conector autorizado | HTTP/18101 | Contrato, credencial, binding, tenant e recibo existentes |
| Relay | MAESTRO | Relay autorizado | HTTPS/18443 | Identidade TLS, credencial escopada e recibo; admissão standby desabilitada |
| Cada ingresso | PostgreSQL existente | Próprio ingresso | PostgreSQL / destino privado | Papel e acesso já autorizados; sem novos grants implícitos |
| Monitor autorizado | Health de cada serviço | Monitor | HTTP/HTTPS | Saúde técnica; não confere autoridade de admissão |

Projetos independentes, uma rede operacional externa por serviço, sem
dependência de lifecycle cruzada, sem publicação de porta no host, sem
build, pull ou migração automática. O plano candidato pressupõe comunicação
roteada e identidade L3 dedicada; L2 apenas se a colocação Macvlan existente
for mantida. Identidade estável é necessária para o bind e a confiança TLS
do MAESTRO; a identidade Andy deve ser escolhida após validar os caminhos
dos produtores. Preferir preservar o endereço TLS atual depois que seu
titular o liberar. Nenhum endereço, rede ou VLAN foi reservado por este
pacote. Não atribuir endereço por ausência de ARP. A existência dos campos
ipv4_address nos modelos não constitui autorização de alocação.

O healthcheck Andy recebe 503 durante manutenção. Isso não reiniciou o
container no ensaio com a política Docker unless-stopped; não prova o
comportamento de supervisores externos. Não usar health como prova de
exclusão de escritores ou de progresso dos ciclos.

## Evidência e condições de promoção

- 25/25 verificações de independência de lifecycle com inicializadores
  reais, incluindo o caminho habilitado do MAESTRO somente em fixture.
- 15/15 verificações da adaptação standby na imagem legada, incluindo
  recusa de TLS ausente, 503 sem escrita, recibo idempotente e retorno a standby.
- 20/20 verificações estáticas dos manifestos; 20/20 verificações de runtime
  via Compose, com override descartável network none, socket de PostgreSQL
  sintético 0067, TLS/credenciais sintéticos e healthchecks acelerados.
- As provas não certificam topologia, endpoints, segredos, papéis,
  recursos, watchdogs externos, rotas ou transição real. Não há prova de
  drenagem do processo compartilhado legado, RPO zero ou tempo máximo de
  indisponibilidade. Não reutilizar a aprovação histórica de #374 como
  certificação da adaptação.

A-PRE exige identificar todos os produtores e caminhos de admissão, provar
retry/spool e recuperação de trabalho inconcluso; reservar identidades por
autoridade; reconstruir inputs e rollback efetivos, revisar permissões
privadas do novo UID, mounts, saúde, limites e backups/restores atuais.
Sem isso, não há A-AUTH. O perfil authorized-cutover e as variáveis
obrigatórias são salvaguardas de configuração, não autorização efetiva.
Não executar up/create/start em produção a partir destes templates.

A0 mantém a exigência de conclusão comprovada do legado. A1 requer
aceite específico de interrupção e risco residual, janela, critérios de
aborto, reconciliação sem replay e rollback. A mudança do processo
compartilhado exige decisão própria. Não sobrepor identidades de rede ou
escritores antigo/novo. O rollback A não é restauração destrutiva do banco.

Mesmo depois da separação, a janela B continua independente. O MAESTRO só
pode ser considerado não escritor mediante evidência de exclusão de
escritas em sua configuração e artefato efetivos e em todos os caminhos.
Não confundir uma amostra ociosa de pg_stat_activity ou gate DRAINED com
prova de exclusividade global. Preservar todos os participantes obrigatórios.
Exigir o hold e as autorizações do runner de schema, backup/restore e
preflight próprios. Não realizar DDL, alterar papéis ou habilitar o canário
por inferência.

Nenhum endereço, credencial, arquivo privado ou configuração de operação
real deve entrar no Git. Os inputs privados são deliberadamente externos
a este pacote.
