# Experimento controlado CI01 × CI02 — distributed-transport

**Issue:** #345 · **Estado:** desenho versionado, nenhuma execução remota/autorização de isolamento de worker.

## O que os checks comprovam

| Execução | SHA | Worker | Resultado |
| --- | --- | --- | --- |
| PR #338 | 687142864d9b045fcfcc2e962da9f995fa680b87 | CI02 | FAIL; teste TypeScript installed-package.mjs:20 |
| PR #344 | 79b72d915bf0b6a6acd97765ef7290f703696848 | CI01 | PASS |

O arquivo de teste tem o mesmo blob Git entre main e #338, mas os SHA dos commits e as máquinas são diferentes. Ainda NÃO é prova de falha intrínseca no CI02. O check distributed-transport é shadow (não required); o distributed-python e o distributed-postgres são required segundo ops/provisioning/distributed-ci-lab/README.md.

## Evidências a preservar ANTES de qualquer reexecução

- Metadados públicos do check original: SHA exato, worker, duração, retorno, classificador, comando, logs sanitizados, Node/npm quando disponíveis.
- Identidade/capability do worker, versão do executor, snapshot de ambiente permitido e versões de lockfiles, sem dados privados.
- Resultado do comando npm pack que gerou packed.files indefinido; falta de evidência deve ser declarada, não reconstruída como fato.
- Localização segura do log e hash do material observado; não alterar nem expor arquivos operacionais privativos em Git.

## Plano do teste de mesmo SHA (pendente de autorização operacional específica)

1. Congelar um único commit escolhido (preferir SHA falho de #338) e a versão dos scripts CI; confirmar esse SHA no início de cada execução.
2. Antes da execução, validar em modo somente-leitura a capacidade disponível e evitar colisões com jobs obrigatórios.
3. Executar a MESMA suíte transport e mesmo SHA em CI01 e CI02, em worktrees descartáveis e com locks normais de worker, registrando npm, Node, arquitetura e ambiente efetivos. Não confiar no agendador automático para assegurar a escolha do host: o script versionado andy-ci-distributed-suite escolhe worker por ranking/offset e não expõe opção de fixação do worker em sua interface pública.
4. Uma invocação direta e controlada do runner por worker deve ser revisada por operador, não improvisada no AGT. Não configurar fallback artificial nem modificar Host Registry para manipular seleção.
5. Se houver resultado divergente, preservar o snapshot e repetir no máximo uma rodada controlada após revisar impacto e orçamento, sem varrer indefinidamente.
6. Comparar por dimensões: SHA, fonte do checkout, versão worker-runner, Node/npm, comando, lockfile, cache, arquivos empacotados e desvio de stdout/stderr.
7. Classificar ao final: REPRODUCED_ON_CI02_ONLY, REPRODUCED_ON_BOTH, INTERMITTENT, NOT_REPRODUCED ou INCONCLUSIVE. A classe descreve observação, não automaticamente causa raiz.

## Fronteiras e critérios

- Não remover CI02 do pool sem evidência independente de risco à integridade das execuções e decisão operacional explícita.
- Não corrigir teste TypeScript nem afrouxar CI simplesmente para obter verde; não declarar problema de cache/npm sem reprodução.
- Não rodar carga pesada no control plane. Limitar chamadas, duração e concorrência; preservar checks required da capacidade compartilhada.
- Não acessar AGT ou workers enquanto não houver autorização para essa fase; este arquivo é somente o protocolo.
- Separar o pacote de fatos técnicos do gabarito reservado ao benchmark; issue #345 é pública e não pode servir como seed cego do agente.
- Não misturar este ensaio com a construção do Investigador: as trilhas prosseguem em branches/PRs independentes.

**Reprodução efetiva:** NÃO REALIZADA. **Isolamento físico/lógico do CI02:** NÃO REALIZADO. **Causa demonstrada:** NÃO.