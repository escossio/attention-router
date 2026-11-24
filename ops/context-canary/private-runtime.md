# Consulta privada do OWNER

A consulta recebe `POST /api/v1/client/context/query` com um bearer de ClientSession, uma pergunta e um `scope_ref` já inscrito. O servidor deriva tenant, OWNER, binding, finalidade e conta, verifica consentimento vigente antes do embedding e faz a leitura em duas transações. Entre elas, E5 local e o compilador ordenam e limitam os trechos. A entrega ocorre dentro da segunda transação após revalidar a sessão, consentimento, epoch, índice e fontes. Um trecho citado é uma observação atribuída à fonte. Na ausência de suporte elegível a consulta é recusada ou devolve abstenção. Nenhum trecho é interpretado como instrução.

## Empacotamento e acesso

O build recebe o SHA completo do Git. O commit fixa o manifesto amd64 da imagem oficial Python 3.12 em `python-base.txt` e o SHA-256 de cada wheel da cadeia de dependências em `requirements.linux-amd64.lock`. A preparação `python3 ops/context-canary/build_private_oci.py fetch-wheels <sha> <diretório>` busca somente distribuições binárias no PyPI. A etapa `build <sha> <diretório> <arquivo.oci.tar>` usa essa pasta em mount e executa a instalação sem rede. O construtor lê o código e o lock do Git, fixa os timestamps e confere hashes dos blobs, plataforma, UID, SHA, comando e OFF antes de devolver o digest do manifesto. O modelo fica fora do build. O serviço exige o manifesto E5 versionado e verifica os hashes dos arquivos ao abrir o provedor.

O workflow manual `.github/workflows/private-context-oci.yml` aceita a main, reconstrói sem cache, confere a igualdade dos manifestos, gera SPDX e grava o arquivo OCI como artifact da execução. O job separado de atestação vincula a proveniência ao digest do manifesto, ao arquivo baixável e associa a SBOM ao manifesto. Antes de uma futura promoção, confira que a execução, a origem assinante e o SHA aprovados correspondem ao manifesto e ao arquivo recebido.

O perfil `dark` inicia em OFF, sem credenciais, modelo, acesso ao banco ou rota de consulta. `/health/live` retorna 200 e `/health/ready` retorna 503 com `disabled`. O perfil `owner` requer a configuração completa da allowlist de um único OWNER e consentimento já persistido no PostgreSQL e os mounts somente leitura do diretório do socket PostgreSQL, diretório do modelo, diretório do anchor independente e diretório exclusivo de segredos. O consentimento é consultado no banco. Os únicos arquivos de segredo necessários são `context_reader_dsn` e `context_epoch_key`. A chave tem pelo menos 32 bytes e é lida sem normalização. O DSN usa psycopg, o papel leitor de G4 derivado do tenant/OWNER/conta e o socket em `/run/postgresql`. O anchor e seu diretório devem permitir ao UID do processo a leitura privada exigida pela autoridade SQLite. Novos anchors usam o journal de rollback padrão e podem ser reabertos após o processo escritor fechar. Um anchor legado em WAL precisa manter os arquivos auxiliares disponíveis no mount somente leitura ou passar por migração de journal em janela própria. A consulta nunca altera o modo do anchor. A verificação do anchor ocorre na consulta de cada escopo. Enquanto ativo, o readiness verifica o papel, a revisão, o fence e a inscrição do tenant em transação curta.

O Compose dá ao processo UID 65532, sistema de arquivos somente leitura e namespace sem rede externa. O cliente autorizado compartilha o namespace de rede privado da instância e inicia TCP em `127.0.0.1:18110`. O leitor inicia PostgreSQL pelo socket Unix montado. A aplicação pública, o provedor remoto e os conectores ficam fora desse grafo. Esta disposição dispensa identidade L2/L3 própria e endereço estático. A execução usa no máximo seis conexões HTTP concorrentes no servidor, dois slots de banco e 32 reservas de encode por janela móvel de 60 segundos por processo. Esses valores são limites configurados. O teste de carga mede seus efeitos.

## Promoção e reversão

O executor `private_runtime_release.py` exige o comentário de autorização na #428 e o digest do artefato. Antes do Compose, ele confere G5/G6, checkout, checks, atestação, digest, label de revisão e entrypoint da imagem local. Passe o arquivo baixado com `--archive`. O pré-voo verifica o atestado do arquivo pelo workflow privado em main, no SHA integrado, e compara o manifesto do arquivo ao digest da aprovação. O operador carrega o mesmo arquivo após o pré-voo autorizado. O executor confere o ID local pelo digest da configuração ou, no armazenamento containerd, pelo manifesto OCI e seu Descriptor. Ele usa o ID conferido no Compose com `--pull never`. As operações de aplicar e reverter conservam suas autorizações próprias.

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
