# Consulta privada do OWNER

A consulta recebe `POST /api/v1/client/context/query` com um bearer de ClientSession, uma pergunta e um `scope_ref` já inscrito. O servidor deriva tenant, OWNER, binding, finalidade e conta, verifica consentimento vigente antes do embedding e faz a leitura em duas transações. Entre elas, E5 local e o compilador ordenam e limitam os trechos. A entrega ocorre dentro da segunda transação após revalidar a sessão, consentimento, epoch, índice e fontes. Um trecho citado é uma observação atribuída à fonte. Na ausência de suporte elegível a consulta é recusada ou devolve abstenção. Nenhum trecho é interpretado como instrução.

## Empacotamento e acesso

O build recebe um SHA completo e uma imagem de dependências Python/E5 de origem aprovada, por digest. O construtor usa `git archive`, plataforma única, dependências existentes verificadas pelo pyproject, timestamps do commit e execução de passos sem rede. Ele grava um arquivo OCI local novo. O registro da execução precisa identificar a versão do BuildKit, base, SHA, plataforma e digest do manifesto OCI. A comparação de duas reconstruções e a atestação do artefato final no SHA integrado são gates pendentes. Os pesos ficam fora do build. O serviço exige o manifesto E5 versionado e verifica os hashes dos arquivos ao abrir o provedor.

O perfil `dark` inicia em OFF, sem credenciais, modelo, acesso ao banco ou rota de consulta. `/health/live` retorna 200 e `/health/ready` retorna 503 com `disabled`. O perfil `owner` requer a configuração completa da allowlist de um único OWNER e consentimento já persistido no PostgreSQL e os mounts somente leitura do diretório do socket PostgreSQL, diretório do modelo, diretório do anchor independente e diretório exclusivo de segredos. O consentimento é consultado no banco. Os únicos arquivos de segredo necessários são `context_reader_dsn` e `context_epoch_key`. A chave tem pelo menos 32 bytes e é lida sem normalização. O DSN usa psycopg, o papel leitor de G4 derivado do tenant/OWNER/conta e o socket em `/run/postgresql`. O anchor e seu diretório devem permitir ao UID do processo a leitura privada exigida pela autoridade SQLite, incluindo os arquivos auxiliares do WAL ao usar o mount somente leitura. A verificação do anchor ocorre na consulta de cada escopo. Enquanto ativo, o readiness verifica o papel, a revisão, o fence e a inscrição do tenant em transação curta.

O Compose dá ao processo UID 65532, sistema de arquivos somente leitura e namespace sem rede externa. O cliente autorizado compartilha o namespace de rede privado da instância e inicia TCP em `127.0.0.1:18110`. O leitor inicia PostgreSQL pelo socket Unix montado. A aplicação pública, o provedor remoto e os conectores ficam fora desse grafo. Esta disposição dispensa identidade L2/L3 própria e endereço estático. A execução usa no máximo seis conexões HTTP concorrentes no servidor, dois slots de banco e 32 reservas de encode por janela móvel de 60 segundos por processo. Esses valores são limites configurados. O teste de carga mede seus efeitos.

## Promoção e reversão

O executor `private_runtime_release.py` exige o comentário de autorização na #428 e o digest do artefato. Antes do Compose, ele confere G5/G6, checkout, checks, atestação, digest, label de revisão e entrypoint da imagem local. A produção depende de um publicador confiável para esta imagem. O workflow de publicação da base atual está fixado em um release anterior. Um arquivo OCI construído em laboratório ainda requer uma cadeia aprovada de publicação e atestação.

| Operação | Alvo | Resultado |
| --- | --- | --- |
| `runtime_apply` (G5) | `dark` | Sobe apenas o processo desligado. |
| `canary_enable` (G6) | `owner` | Requer aprovação específica, material de G3/G4 e consentimento/epoch explícitos. O cliente precisa verificar readiness e autorização corrente antes da primeira consulta. |
| `canary_disable` (G6) | `owner` | Para a instância autorizada pelo digest. |
| `runtime_rollback` (G5) | `dark` | Para a instância OFF correspondente. Desativar o canário por G6 antes de completar o rollback de G5. |

## Matriz de aceite descartável

Os nomes abaixo identificam testes e a camada coberta. As provas com banco exigem CI03 no SHA da candidata, modelo e manifesto fixados. A coleta de casos não substitui execução. A matriz ainda precisa de uma rodada integrada do contêiner sob o papel instalado por G4.

| Caso | Teste ou prova existente |
| --- | --- |
| 1. OWNER recupera fonte vigente | `test_owner_enrollment_durable_epoch_e5_http_revocation` exercita E5 e HTTP privado, fonte citada e correlação. |
| 2. Isolamento de tenant | `test_authenticated_context_query_across_processes` recusa ClientSession de outro tenant. |
| 3. Isolamento de conta | `test_candidate_eligibility_and_concurrent_withdrawal` e `test_minimal_roles_restrict_tenant_account_owner_and_no_bypass` exercitam recuperação e RLS. |
| 4. Sessão expirada | `test_authenticated_context_query_across_processes` expira a primeira sessão e verifica a recusa. |
| 5. Consentimento ausente | `test_grant_is_explicit_scoped_and_idempotent` e registro OWNER exercitam escopo e decisão por fonte. A preparação da consulta exige ao menos uma fonte válida. |
| 6. Revogação prévia | `test_grant_is_explicit_scoped_and_idempotent` e os testes do API de consentimento verificam retirada e consulta seguinte. |
| 7. Revogação durante cálculo | `test_owner_enrollment_durable_epoch_e5_http_revocation` retira a fonte no compilador e exige resposta vazia. |
| 8. Correção | `test_authenticated_context_query_across_processes` altera a conclusão posterior à rejeição do insight. Teste do adaptador semântico de laboratório. |
| 9. Nova sessão | O mesmo teste reabre ClientSession e consulta o contexto em outro processo. |
| 10. Contradição | O mesmo teste verifica duas datas conflitantes. `test_grounded_excerpts_and_abstention` cobre duas citações divergentes e conclusão em aberto no renderizador privado. |
| 11. Sem suporte | Os mesmos testes exigem abstenção e ausência de afirmação livre. |
| 12. Zero efeitos | O teste multiprocesso compara contadores de efeitos e verifica a recusa SQL para Outbox, intents e grants. A fixture E5 compara os contadores de efeitos ao finalizar. |

O processo usa correlação aleatória por consulta e registra somente estados de decisão, contagens e tempo decorrido. O número de candidatos e fontes sem autorização é medido em cada passagem, incluindo a revalidação. Uma mudança de revisão de consentimento bloqueia a entrega. O evento de revogação é emitido quando há leitura de um tombstone de consentimento. Um timeout ou falha de banco/índice registra a classe da falha, sem parâmetros. O progresso do escritor é medido por transações de escrita no laboratório. `test_source_and_enqueue_writers_conflict_by_tenant` e `test_vector_worker_requeues_fence_contention_without_poisoning` exercitam esse contrato. A comparação pareada 200×16 com fontes, permissões e política E5 fixas precisa medir latência, bloqueios e progresso do escritor. Revisão semântica M1 e dispositivo Android físico têm gates próprios.
