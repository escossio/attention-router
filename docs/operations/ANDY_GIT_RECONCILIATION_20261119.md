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

## Critério de publicação e cutover

PRs de reconciliação devem começar em modo draft. Merge, publicação
de APK, alteração de migração em produção, recriação de contêineres e
corte de serviços exigem gates próprios e aprovação posterior.
Não publicar arquivos .env, credenciais, cookies, contatos, conversas,
áudios ou dados de localização.

A presença de código funcional em Git **não comprova**, por si só,
equivalência binária com imagens anteriores. Antes do cutover é necessária
uma construção reproduzível e comparação de serviços, contratos e dependências.
