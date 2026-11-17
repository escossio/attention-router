# Investigador V0 — Protocolo de evidência nominal do F01

**Janela de governança:** [#356](https://github.com/escossio/attention-router/issues/356) · **Caso:** `F01` · **Registro:** COL-20261015-019/020.
**Estado:** PROPOSTO / SEM EXECUÇÃO ADICIONAL. Não autoriza comandos nos workers, testes manuais, alterações do motor, deploy ou merge.

## 1. Objeto da coleta

Demonstrar separadamente o que aconteceu aos testes abaixo no PR [#357](https://github.com/escossio/attention-router/pull/357), HEAD exato **`b6fc9e1f26be53cfb25e473c2d3e78613e5047c1`**:

- `test_f01_after_hmac_before_db_consumes_nonce_without_durable_seed`: **observacional**. Injeta `RuntimeError('F01_SYNTHETIC_BEFORE_DB')` no stub de `store.stage_verified`, após a verificação HMAC. O teste espera zero seeds/nonce/audit persistidos, rejeição de retry com a mesma instância e capacidade da nova instância de registrar uma vez.
- `test_f01_required_same_instance_retry_after_abort_is_recoverable`: **hipótese normativa ainda não ratificada**, decorada `@pytest.mark.xfail(strict=True)`. Ela tenta reusar o mesmo envelope/nonce após uma falha pré-commit. Um XFAIL não prova que reaceitar o mesmo nonce é uma política de segurança desejável.

**Premissa essencial:** antirreplay e recuperação do evento são contratos separados. É possível exigir que o mesmo nonce permaneça inutilizável e, simultaneamente, exigir recuperação por nova requisição assinada com nonce novo ou consulta autenticada de recibo.

## 2. Evidência já obtida

| Evidência | Valor | Classificação |
| --- | --- | --- |
| `distributed-postgres`, SHA acima | SUCCESS no CI03, 476 tests passed, 58 files | CONFIRMADO no agregado |
| Comparação com PR #355 | 475 tests passed antes de adicionar F01 | INDÍCIO de um novo PASS; não é rastreio nominal |
| Check annotations | `0` em todos os quatro checks | NENHUMA anotação individual publicada |
| GitHub Actions pull-request runs | Nenhum run retornado pela consulta por SHA | NÃO LOCALIZADO por essa interface, não ausência universal |
| Catálogo de artefatos GitHub | Consulta de metadados recusada pelo conector | INACESSÍVEL por esse caminho |
| Relatório individual PASS/XFAIL | Não disponível na superfície consultada | NÃO VERIFICADO |

Estes dados vieram de consultas somente-leitura sob COL-019. Não houve rerun, acesso aos hosts, download binário ou alteração de fonte para obtê-los.

## 3. Coleta futura — somente após autorização específica

**Pré-condição:** publicar nova ficha `COL-...` com operador, comando revisado, SHA 40 hex, ambiente/DB descartável, worker selecionado pela infraestrutura já autorizada, capacidade disponível, timeout, custo, política de logs, mecanismo de rollback e credenciais puramente sintéticas.

**Escopo dos casos:** executar somente os dois testes F01 do arquivo `tests/integration/test_postgres_investigator_durable_admission_v0.py` do SHA acima. A ferramenta poderá usar seleção `pytest -k 'test_f01_'` e gerar JUnit XML. A formulação é uma *proposta*, não um comando para execução imediata.

**Indicadores exigidos na saída nominal:**

1. Identidade e SHA do worktree confirmados ANTES de `pytest` (evitar código mutado/branch flutuante).
2. Worker, versão Python/pytest, ID da execução e perfil do banco de testes criado exclusivamente pela fixture descartável do repositório.
3. Linha nominal PASS/FAIL/ERROR/SKIP/XFAIL/XPASS para **cada um** dos dois testes; `pytest -rxX` ou equivalente, com status final e tempo por caso.
4. Totais separados de `passed`, `failed`, `error`, `xfailed` e `xpassed`; sucesso da suíte NÃO substitui esta decomposição.
5. Relatório JUnit sanitizado ou artefato equivalente com vínculo ao SHA/test IDs, hash dos arquivos coletados e política de retenção. Não publicar payload de evento, nonce operacional, chaves, hosts internos ou credenciais.
6. Resultado do estado de prova: contagens pós-falha de seeds/nonces/audit; comportamento em retry na mesma instância e em nova instância, com a natureza **sintética** expressamente indicada.

**Critérios de parada:** arquivo ou SHA divergente, banco que não seja descartável, processo tentando conectar ao AGT/produção, segredo nos logs, falha da fixture, desvio de capacidade do scheduler, ausência de resultado nominal, timeout ou qualquer tentativa de remediação automática.

## 4. Interpretação e governança

| Cenário futuro | Interpretação permitida | Decisão |
| --- | --- | --- |
| Observacional PASS + normativo XFAIL | Comportamento de estado reproduzido na fixture e proposta de retry não atendida | Abrir decisão de contrato, sem considerar segurança insuficiente automaticamente |
| Observacional PASS + normativo XPASS | Contradição à previsão: mudança inesperada ou semântica de teste inadequada | INCONCLUSIVO até revisar o caso; não apagar o registro |
| Observacional FAIL ou ERROR | Teste F01 não reproduziu os estados esperados / infraestrutura falhou | PRESERVAR, investigar sem corrigir durante coleta |
| Ambos ausentes de log nominal | Relatório agregado insuficiente | SEM CERTIFICAÇÃO NOMINAL; manter gate aberto |

**Significado do XFAIL:** não é evidência de que a correção deva simplesmente permitir reutilização da mesma assinatura. A meta real é recuperação idempotente, autenticada e observável, com antirreplay preservado. A escolha entre nova assinatura/nonce, recuperação autenticada de recibo ou claim transacional deve passar por ADR própria.

## 5. Limites definitivos desta rodada

Não rodar `pytest` por chat, AGT ou CI sem ficha autorizada. Não adicionar manualmente um quarto argumento ao runner nem escolher worker por conveniência. Não trocar código de produção, não criar migrações, não ativar OpenAI Agents SDK nem mudar infraestrutura de rede. O protocolo torna a evidência coletável; **não afirma que ela já foi coletada**.

**Decisão da janela:** NO-GO para produção e GO somente para revisão documental e futura avaliação controlada explicitamente autorizada.