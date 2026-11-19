# Checkpoint de reconciliação Andy / AGT ↔ Git (19/11/2026 UTC)

**Escopo:** código do Attention Router, API, Worker, Transport, conversas,
Google Contatos, histórico de localização, índice semântico híbrido e SDK
Android. Os demais serviços do AGT não estão incluídos nesta certificação.

## Bases do GitHub

- escossio/attention-router, main: ceadb677225efe6fe7633705b9c5bab162154fca
- escossio/andy-android, main: d06b9bdfcdb7dc4bddc842a4131c3a705f02b659
- Branch candidata para cada repositório: integrate/agt-reconcile-20261118

A reconciliação foi feita sem sobrescrever checkouts anteriores e sem
reiniciar contêineres produtivos. As divergências de código-fonte foram
comparadas com arquivos efetivamente encontrados nas imagens operacionais.
Os detalhes verificáveis das imagens estão em
ops/reconciliation/runtime-images-20261119.json.

## Fontes integradas

Foram considerados: recepção prospectiva bilateral do WhatsApp,
classificação de origem da mensagem, captura/transcrição de voz sem autoridade
de execução, enriquecimento conversacional, localização com hora original,
Google Contatos com permissões separadas e memória vetorial passiva com
relevância híbrida e recuperação por proveniência.

Os segmentos semânticos não são fontes de autorização nem conhecimento
autocertificado. As transcrições que não forem verificadas não são
promovidas. A camada de extração cognitiva permanece desligada.

## Migrações

A cadeia da base operacional é:

    0061_tts_outbox_constraint
    0062_location_history
    0063_conversation_context
    0064_context_owner_scope
    0065_conversation_vectors
    0066_vector_owner_scope
    0067_vector_lock_order

A candidata 0068_google_contacts_cache formaliza duas tabelas já existentes,
criadas anteriormente por instalador aditivo. A migração foi validada em
PostgreSQL 16 descartável com inserção, FK, idempotência, recusa de downgrade
com dados e downgrade seguro de base vazia.

Há outro ramo com a revisão 0062_artifact_sensitive_review_v1.
Ela não deve ser aplicada nem renumerada automaticamente: o conflito
exige reconciliação e teste específico, separados deste PR.

## Gates executados localmente

- Banco e pgvector isolados no CI01: 42 testes aprovados; migração integral
  até a cabeça, avaliação vetorial, downgrade e novo upgrade aprovados.
- Transport: 299 testes Node aprovados; nenhuma falha.
- Backend (cenários focados): 70 aprovados, três casos legados
  explicitamente descontinuados.
- Model downloader: arquivo de revisão e digests SHA-256 versionado,
  pesos não versionados; verificador de hashes exercitado em testes sem rede.
- Android CI03: compilação direcionada, testes de SDK e onboarding e
  assembleDebug concluídos. Nenhum APK instalado ou publicado.
- Sintaxe Python e JavaScript, Ruff e diff-check aplicados à candidata.
- Heurísticas de segredo sem achados de chaves privadas ou tokens válidos;
  testes contêm dados sintéticos, sem contatos reais.

A suíte geral do backend e os checks oficiais do GitHub são gates adicionais,
não substituídos pelos resultados focados.

## Política forward-only

A Andy deve capturar mensagens, mídias e contexto a partir da utilização do
aplicativo. O replay histórico é opcional, desabilitado por padrão e
**não** pertence ao caminho obrigatório de instalação em novos dispositivos.
Os três testes antigos exclusivamente de replay ficam marcados como SKIP,
não contados como PASS. O STT prospectivo permanece funcional.

Atualização operacional em 19/11/2026, 00h40 UTC: foi aplicada no AGT
uma sobreposição Compose que desabilita somente o backfill histórico
(WHATSAPP_VOICE_BACKFILL_ENABLED=false). O worker reiniciou na MESMA
imagem e voltou ao estado healthy com zero reinicializações automáticas.
STT_ENABLED=true e CONVERSATION_CAPTURE_ENABLED=true foram preservados,
assim como os IDs dos cinco serviços correlatos conferidos. Nenhum
trabalho histórico estava em processamento antes da operação.
O arquivo worker.forward-only.compose.example.yml reproduz apenas a
opção pública, sem copiar os arquivos privados de configuração.
Os novos áudios continuam elegíveis ao STT; nenhuma captura orgânica
posterior ao cutover foi necessária para comprovar os gates de configuração.

## Fechamento dos gates distribuídos e do aplicativo (19/11 UTC)

- Android: PR de governança #100 e PR de produto #101 foram integrados
  à main de escossio/andy-android. Commit final da main:
  e1326aaef977c42a55afe9987a633a591ae4cfd7.
  Nenhum APK foi instalado ou distribuído por essa reconciliação.
- PR backend #417 continua independente, sem merge prematuro.
- A falha do CI distribuído em 4e96890 revelou que o CI03 estava
  utilizando postgres:16-alpine sem a extensão pgvector. Foi preparada
  seleção de uma imagem PostgreSQL 16 com pgvector 0.8.7 fixada por digest
  e verificada em contêiner de teste. Para código antigo, a imagem original
  continua a ser utilizada.
- O CI03 ficou inacessível pela rede durante os testes. A governança da rede
  identifica CI01 como postgres-worker elegível. O failover deve usar o
  controle da capacidade existente; não publicar checks verdes sem executar
  o conjunto de testes sobre o SHA exato e registrar a evidência.
- A suíte antiga do banco incluía comparações com 0061 como último head.
  Os casos de teste passam agora a determinar e exigir a cabeça única
  real do Alembic, sem alterar nem contornar as migrações.
- A fixture de identidade limpa explicitamente as tabelas derivadas,
  sem adicionar TRUNCATE CASCADE genérico.
- Dependências locais de embeddings foram declaradas como extra
  opcional de desenvolvimento e verificadas por digest/model manifest.
  Nenhum texto privado precisa ser transmitido para criar vetores.

## Certificação adicional da candidata (19/11 UTC)

- No CI01 isolado, a sequência completa de migrações até a cabeça
  `0068_google_contacts_cache` terminou com sucesso. A execução integral
  dos testes marcados `postgres` aprovou **675**, com **3.277 não selecionados**
  e oito avisos; não representa aprovação dos testes não selecionados.
- Os 21 arquivos de testes alterados ou adicionados conferidos nessa
  execução coincidem por SHA-256 com a árvore local de reconciliação.
  Ruff no fonte completo da candidata e checagens de sintaxe passaram.
- A imagem PostgreSQL 16 com pgvector foi pré-carregada por digest e o
  manifesto do modelo ONNX local foi confirmado por SHA-256.
- O check GitHub `distributed-postgres` na cabeça anterior reprovou ao usar
  imagem sem `pgvector` no CI03. A aprovação local do CI01 não deve ser
  publicada como check oficial; este depende de execução do SHA final.
- CI03 encontra-se inacessível no momento desta checagem; a troca
  controlada de worker deve manter rollback e registrar a execução real.

## Critério de publicação e cutover

PRs de reconciliação devem começar em modo draft. Merge, publicação
de APK, alteração de migração em produção, recriação de contêineres e
corte de serviços exigem gates próprios e aprovação posterior.
Não publicar arquivos .env, credenciais, cookies, contatos, conversas,
áudios ou dados de localização.

A presença de código funcional em Git **não comprova**, por si só,
equivalência binária com imagens anteriores. Antes do cutover é necessária
uma construção reproduzível e comparação de serviços, contratos e dependências.
