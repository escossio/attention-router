# Investigador V0 — prova de admissão transacional PostgreSQL

**Estado:** PROPOSED / TEST-ONLY. **Código candidato:** PR #355, derivado de #353. **Contrato:** AR-OPS-INV-V0, PR #344.

## Limite da prova

O objetivo do PR #355 é provar em PostgreSQL descartável a atomicidade do primeiro recebimento de um evento sintético já verificado por HMAC. O mesmo commit transacional deve registrar:

- Identificação de principal/fonte/tenant derivada de binding sintético confiável.
- Bytes originais recebidos, hash SHA-256, identidade única do evento.
- Uso de nonce por principal para negar replay entre processos/instâncias.
- Primeira linha de auditoria sem copiar payload, prompt ou segredo.

Uma recepção com **novo nonce e mesmo evento/bytes** retorna o seed previamente gravado e não autoriza nova investigação. Um novo nonce com **mesma identidade e bytes diferentes** é conflito terminal e não grava segundo seed nem segundo nonce. Um nonce reutilizado falha fechado. Um evento com assinatura inválida é rejeitado antes do acesso ao banco.

## Delimitação da infraestrutura

Os testes usam `pg_url` do diretório `tests/integration`: a própria suíte cria `attention_router_test_<10hex>` e destrói esse banco após os testes. O módulo recusa URLs que não tenham driver PostgreSQL/psycopg, usuário sintético, DB descartável e schema de teste explicitamente nomeado. A criação/remoção de schema ocorre **exclusivamente em fixture**.

Não adicionar o schema ao Alembic principal, não provisionar credencial/role, não alterar o PostgreSQL existente no AGT. O `distributed-postgres` executa a suíte nos workers autorizados; o AGT atua apenas como control plane de CI, sem executar suíte pesada local.

## Ensaios específicos

| Prova | Resultado requerido |
| --- | --- |
| First write | seed, nonce e auditoria visíveis juntos após commit em outra conexão |
| Replay entre instâncias | mesma assinatura/nonce, verificada em outra instância, falha na unicidade PostgreSQL |
| Retry legítimo | nonce novo + bytes idênticos = mesmo seed; somente uma nova linha de auditoria de retry |
| Conflito de bytes | mesmo principal/evento com conteúdo diferente = rollback e primeira evidência preservada |
| Concorrência | duas admissões simultâneas do mesmo evento com nonces distintos geram apenas um seed |
| Falha no audit insert | seed e nonce da mesma tentativa são revertidos junto com o erro |
| Tenant diferente | não pode ler original nem acrescentar audit ao seed de outro tenant |
| Auditoria segura | apenas códigos/fingerprints; payload e textos livres recusados |

## Lacunas preservadas

Esta não é prova de durabilidade após desligamento físico do banco: o PostgreSQL descartável do CI pode usar fsync desabilitado por projeto. A prova confirma atomicidade de transação e visibilidade entre conexões, não backup/restore, replicação, qualidade de armazenamento nem recuperação após pane elétrica.

A autoridade HMAC em memória consome nonce antes do commit no store. Se o commit falhar, o retry na mesma instância poderá ser negado antes de chegar ao DB. O mecanismo deve ser redesenhado para coordenação exclusivamente durável antes do uso real. A persistência de cada chamada/ferramenta, hipótese revisada e conclusão ainda requer implementação adicional e acesso segregado.

Os testes precisam permanecer em banco temporário, nunca no ambiente operacional. Adicionar novo arquivo de teste PostgreSQL pode demandar reprofile do laboratório; não contornar o gate rodando a suíte pesada no AGT.

## Próximo gate

Certificar o PR #355 no SHA exato; demonstrar auditoria durável para todo o ciclo de investigação, isolamento de roles, retry após crash, política de retenção e orquestração de modelos sob orçamento separado. Nada disso é autorizado automaticamente pelo teste de admissão.