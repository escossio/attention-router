# Andy: reconstrução rastreável, sem substituir a produção

Esta pasta fornece uma receita de rebuild candidato da recuperação semântica
local. Não deve ser utilizada para recriar contêineres produtivos sem validação
de equivalência e procedimento de rollback.

## Imagens e origem

A imagem base é o Dockerfile na raiz do repositório, com fontes integradas.
A variante vetorial depende também de onnxruntime==1.23.2,
tokenizers==0.22.1 e numpy==2.3.5. A indexação e a política híbrida
já estão na árvore attention_router/ da integração.

Exemplos de build em ambiente CI isolado, sem credenciais operacionais:

    docker build -f Dockerfile -t attention-router:reconciled-app-local .
    python ops/reconciliation/download_model.py ops/reconciliation/model
    docker build -f ops/reconciliation/Dockerfile.vector \
      --build-arg APP_IMAGE=attention-router:reconciled-app-local \
      -t attention-router:reconciled-vector-local .
    docker build -f ops/whatsapp-container/Dockerfile.transport \
      -t attention-whatsapp-transport:reconciled-local .

Pesos de modelo NÃO entram no Git. model-manifest.json fixa a revisão e os
SHA-256 esperados. O download verifica tamanho e hash antes de aceitar.
O build vetorial e o runtime verificam os mesmos arquivos.

## PostgreSQL

As migrações presentes em produção seguem:

    0061 -> 0062_location_history -> 0063_conversation_context
         -> 0064_context_owner_scope -> 0065_conversation_vectors
         -> 0066_vector_owner_scope -> 0067_vector_lock_order

A migração candidata 0068_google_contacts_cache reproduz as tabelas
existentes do Google Contatos, antes instaladas por script operacional.
Testá-la em PostgreSQL descartável antes de promover versão da base.

A candidata 0062_artifact_sensitive_review_v1 de outro worktree NÃO
foi integrada: conflita com 0062_location_history. Ela exige rebase,
novo ID, análise de dependências e gate específico.

O pgvector depende da ABI adequada para PostgreSQL 16. Não alterar a
imagem ou recriar o banco produtivo para adicionar a extensão.

## Privacidade, configuração e operação

- Memória cognitiva e autoridade de execução continuam desligadas por padrão.
- Para novos usuários, WHATSAPP_VOICE_BACKFILL_ENABLED=false: a memória
  começa com o uso da Andy, sem obrigatoriedade de recuperação antiga.
- Captura e transcrição de áudios NOVOS independem do backfill.
- Busca semântica não é prova de fatos nem autoriza ações.
- Endpoints privados preservam os mecanismos de autenticação.
- Nenhuma credencial, token OAuth, arquivo .env, cookie, contato, conversa,
  áudio, localização ou segredo de banco deve entrar neste Git.
- O indexador permanece sem porta pública e sem permissão de escrita
  nas mensagens originais.
- UNKNOWN_FROM_ME não comprova autoria humana.

## Gates para integrar

1. git diff --check, revisão de dados privados, código e licença.
2. Testes Python de contatos, localização, captura prospectiva, áudio e híbrido.
3. Testes Node do Transport com fixtures e dependências completas.
4. Alembic upgrade/downgrade/upgrade em PostgreSQL+pgvector isolado.
5. Testes e compilação Android no CI03, sem instalar ou publicar.
6. Paridade de variáveis runtime, imagens, hashes e estratégias de rollback.
7. PRs revisados e checks oficiais do GitHub.
8. Cutover produtivo somente por serviço, com backup e revalidação.

A reconstrução dos fontes NÃO garante equivalência binária com imagens
produtivas. O CI e a operação precisam comprovar essa paridade.
