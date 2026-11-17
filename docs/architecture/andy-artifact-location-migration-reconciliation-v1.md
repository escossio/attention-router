# Reconciliação isolada de Artifact review e localização

PR #404 (`018fa48d8040d9e4fb8d76f8febb3ca361a9edc4`) e PR #408 (`e4925da71f3d16093649b94204091710ee745723`), ambas sobre main `ceadb677225efe6fe7633705b9c5bab162154fca`, criavam revisões distintas 0062. Esta candidata compõe os contratos em laboratório, sem commit, merge, deploy ou ativação.

Cadeia única: `0061_tts_outbox_constraint` → `0062_artifact_sensitive_review` → `0063_location_context_v1`. O corpo da migração GPS não muda; somente nome de arquivo, revision e down_revision. Nenhum dado é importado das localizações antigas. Não há migração 0064 de consentimento nesta trilha.

Os 19 conflitos entre as PRs eram asserts de head. As duas alternativas foram comparadas após normalizar apenas a expectativa de head; seus outros textos eram idênticos. Os asserts existentes de estrutura, constraints e recusas de downgrade continuam preservados. Também foram atualizados testes de head de Artifact/GPS, a referência ao arquivo GPS renumerado e o teste estrutural das waves, que agora verifica ambos os encadeamentos.

`tests/integration/test_postgres_reconciled_artifact_location.py` reutiliza o banco descartável das migrações V2E. Parte de 0061 com Artifact sintético, sobe para 0062, cria revisão, sobe para 0063, acrescenta GPS fictício, comprova que downgrade com GPS é recusado, remove apenas a fixture, retorna a 0062 e comprova que downgrade com revisão é recusado. Após remover apenas essa fixture, um ciclo vazio preserva o Artifact original sem reimportar GPS/revisões.

Essa renumeração é candidata para integração das PRs ainda não incorporadas à main. **Não é procedimento para banco que já tenha aplicado `0062_location_context_v1`**: esse caso exigiria reconciliação explícita do histórico aplicado, inspeção e autorização próprias. Não se altera a tabela alembic_version manualmente e não se enfraquece a proteção contra perda de dados.

As rotas de localização da PR #408 conservam ClientSession/OWNER e consentimento próprios, além da flag desabilitada por padrão. D06-A/D08-A aprovam arquitetura de memória em isolamento e não autorizam coleta de GPS, agenda ou conversas reais. Android e sua fronteira continuam em governança separada.

A trilha permanece separada da integração de áudio/memória. As evidências no AGT incluem manifestos completos, SHA de árvore, SHA-256 dos snapshots, imagens dos workers, logs e JUnit. Nenhum resultado de snapshot é apresentado como certificação formal de um SHA publicado.
