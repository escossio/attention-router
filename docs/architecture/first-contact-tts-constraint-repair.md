# Passada 21A — restrição da outbox para áudio complementar

## Causa e decisão

A migration 0007 criou `uq_outbox_messages_execution_intent`; ela continuou válida até 0060. O modelo SQLAlchemy omitia essa unicidade. Assim, o teste SQLite aceitava texto e áudio para o mesmo intent, mas o PostgreSQL rejeitava o segundo insert. A falha escapava da derivação e encerrava o ciclo do Worker.

A migration incremental 0061 adiciona `supplemental_parent_outbox_id`. Valores nulos identificam todos os registros legados como efeitos principais, sem alterar dados. A unicidade principal passa a ser um índice parcial por `execution_intent_id` com pai nulo. Outro índice parcial admite no máximo um complementar por intent. A FK composta `(supplemental_parent_outbox_id, execution_intent_id)` exige que o pai exista e pertença ao mesmo intent; o CHECK limita o complementar a `agent_execution_voice`, `local_transport` e à chave `execution:<intent>:audio`. A unicidade global da chave de idempotência permanece. Só depois da criação dessas proteções a migration remove a constraint antiga.

O aplicativo liga o áudio ao texto entregue e revalida antes do envio: snapshot/hash, destinatário, interaction, causation, artifact, flags, autoridade corrente e estado `DONE` do texto. Para INTRODUCE, exige apresentação e tentativa `CONFIRMED`, com ACK autenticado >= 2. Estado `AMBIGUOUS` não autoriza áudio nem repetição de texto. Voz como efeito principal mantém pai nulo e a chave `execution:<intent>`.

O Worker isola exceções do ciclo TTS. A derivação que falha no commit é marcada `FAILED` após rollback; o efeito de texto confirmado não é reaberto. Exceções residuais são contidas no limite do ciclo, preservando a próxima iteração. Nenhuma derivação histórica `FAILED` é reativada ou preenchida automaticamente. Os defaults `TTS_AUTO_SEND=false` e `TTS_SUPPLEMENTAL_ENABLED=false` permanecem.

## Reversibilidade e validação

O downgrade restaura a unicidade original apenas quando não há linhas complementares; se houver, falha antes de descartar a coluna. A reconciliação dessas linhas exige decisão operacional separada, especialmente para efeitos ambíguos. A migration não foi executada nesta worktree.

Os testes SQLite cobrem o caminho sintético e erro de commit. O gate PostgreSQL descartável precisa executar `tests/integration/test_postgres_tts_supplemental_constraint.py`, verificando texto mais áudio no mesmo intent, duplicatas, ações não autorizadas, FK e schema migrado. Este gate não foi rodado no AGT: a política exige SHA publicado para `andy-ci-distributed <sha> postgres`, enquanto esta passada proíbe commit e push.

Continuidade ao vivo: novos eventos autorizados continuam pelo fluxo usual, sem mudança de cursor ou ampliação de autoridade. Aceleração histórica: nenhuma importação ou backfill foi implementado; reprocessar as derivações históricas seria inseguro sem conciliação do ledger e dos estados ambíguos.

## Compatibilidade da cadeia Alembic

A revisão ativa após `upgrade head` passa de `0060_first_contact_v1` para `0061_tts_outbox_constraint`. As asserções de *head* em testes de integração e migração foram ajustadas sem alterar a sequência histórica: o novo `down_revision` permanece `0060_first_contact_v1`. Como os testes de downgrade PostgreSQL operam de forma transacional, uma recusa de downgrade mantém a versão ativa `0061`. A regressão local (incluindo os testes de identidade, First Contact, voz, repetição e cadeia Alembic) passou em **187 testes**; Ruff, compileall e `git diff --check` também passaram. Os três novos casos PostgreSQL ainda dependem da certificação distribuída no SHA publicado.
