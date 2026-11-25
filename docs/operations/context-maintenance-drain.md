# Barreira cooperativa de manutenção G3 (candidata, não é autoridade)

## Motivação

A amostra de sessões ociosas não prova que os escritores deixaram de
admitir trabalho. Preservar spool, idempotência, claims, leases, RLS e
proteção de resultados externos desconhecidos. Não alterar schema, regras de
autoridade, seleção de trabalho, semântica do contexto ou filas para simular
uma drenagem. A barreira não envia mensagens nem executa DDL.

## Contrato

Sem a variável ANDY_MAINTENANCE_GATE_DIR, o caminho normal é mantido. Com
a variável presente, cada participante usa a mesma pasta privada absoluta,
previamente inicializada, no mesmo host. Uma pasta inválida ou ledger
indisponível recusa a admissão; jamais inicializar no startup do runtime.
O operador provisiona a lista exata de participantes, o mesmo UID, montagem
e inode estável de control.lock. Usar disco local; não afirmar equivalência
com NFS, armazenamento distribuído ou diferentes hosts. A pasta/estado têm
modo 0700/0600. A coordenação é cooperativa, não uma fronteira contra root
ou um processo que deliberadamente ignore a barreira.

- RUNNING: a reserva de trabalho é persistida sob flock antes da execução.
- DRAINING: geração nova, nenhuma admissão nova; ciclos já reservados
  completam. Participantes ausentes ou reservas ainda existentes bloqueiam.
- DRAINED: roster observado e zero reservas; claim_maintenance estabelece
  uma posse exclusiva persistente da janela. Não é uma aprovação de release.
- Timeout não libera reserva ou reabre a admissão. Depois de terminar
  o trabalho, resume exige janela correta, zero reservas e nenhuma posse.
  release_maintenance exige token da posse, é explícito e não reabre.
  Reinício não recria o ledger. Processo morto, falha não conciliada ou
  token perdido exigem investigação e decisão humana; não há reap
  automático nem API de limpar/replay de reservas desconhecidas.

O ledger mede as reservas dos participantes integrados, não a existência de
transações de todos os clientes, nem confirma o resultado de cada efeito
externo tratado internamente pelo worker. Uma reserva órfã permanece pendente
mesmo sem processo vivo. A observação de um participante registra que ele
alcançou o gate, não uma prova de liveness ou cobertura após recriação do
contêiner. A integração futura deve conferir imagem, identidade, mount,
configuração e roster efetivos e a continuidade das filas; nenhum cliente
pode omitir a configuração. Não aceitar mera contagem zero como autoridade.

## Pontos de admissão da candidata

- worker: ciclo completo, da primeira sondagem à reconciliação final e
  fechamento das sessões, preservando ordem e corpo de processamento.
- voice-backfill, vector-cycle, contacts-cycle e conversation-context-cycle:
  cada ciclo de background, inclusive writes de recuperação de falha.
  Incluir no roster somente os ciclos efetivamente habilitados.
- api-http: requisições, dependências e seed de startup.
- vector-http: requisições do serviço vetorial.
- internal-ingress-http e internal-ingress-executor: a reserva do executor
  pertence à thread que efetivamente trabalha, não ao future que o aguarda.
- neutral-ingress-http: aplicação de ingresso versionada, sem tocar em
  outros listeners. Retorna o contrato existente INGRESS_UNAVAILABLE/503
  ao produtor neutro; transporte WhatsApp recebe erro retentável.
- artifact-worker: candidata versionada para o runner standalone existente;
  ainda não substitui o runner montado no ambiente operacional.

Quando configurada, a barreira conserva a reserva HTTP até finalização ASGI,
incluindo cleanup e requisição cancelada. O executor separado mantém a
reserva até concluir. Respostas 503 não declaram aceitação da mensagem.
Health HTTP também retorna indisponibilidade enquanto fechado; o plano de
operação deve distinguir manutenção de falha e impedir restart por engano.
Não há necessidade de matar o processo para chegar a DRAINED. A persistência
e fsync por admissão têm custo e não foram caracterizados sob carga
operacional; não habilitar por padrão. Timeout de flock é limitado, não
garante latência máxima de syscall de disco sob falha de armazenamento.

## Fronteira G3

O gate foi concebido para teste isolado. Não implementa autoridade de
migração, não altera release_schema.py nem sua política. Antes de uma
eventual janela real: certificar todos os componentes/artefatos e vínculo
do hold ao executor G3, comprovar roster e clientes remotos, transações e
estado de efeitos/filas, validar retomada de ingressos e plano de rollback,
obter decisões específicas. O controle de outro produto, em especial
MAESTRO, não é parte deste patch. Se o serviço compartilhado ou outro cliente
fora do escopo impedir exclusão de concorrência, abortar; não modificar ou
reiniciar tal serviço por inferência. Renovar backup/restore e aceite quando
necessário pelo contrato de promoção. Sem mudança do banco, roles, OWNER ou
G7.


## Composição obrigatória com o runner G3

A candidata acrescenta `execution.lock` (inode estável) ao ledger de
manutenção; o formato do estado local passa a V2. Não há gate anterior
implantado que precise ser convertido. Um estado de outra versão é
recusado, jamais reinicializado no startup.

O documento de aprovação privada do schema mantém a versão 1 e os campos
de backup, restore, catálogo, ACL, banco, operação, confirmação e digest
existentes. Requer também `maintenance_gate` com exatamente `gate_id`,
`window`, `hold` e `participants` (lista ordenada). A aprovação só pode
referenciar um hold já adquirido para a geração drenada. O roster mínimo
desta topologia tem oito participantes obrigatórios; os três ciclos
condicionais integram a lista quando habilitados. Uma lista não é prova
operacional de que todas as instâncias usam o gate.

O `approval_ref` de G3 passa a ser o SHA-256 da serialização JSON canônica
(sort_keys, separadores compactos, UTF-8, sem NaN) do documento privado
excluindo o próprio campo `approval_ref`. A referência opaca já existente
no registro público V2 vincula assim também a posse da janela e o backup.
O formato público do V2 e a exigência de aceite técnico separado não
mudam. G4 não recebe o novo contrato de execução nesta passada.

O runner mantém a verificação GitHub antes de abrir o banco. Depois confere
o documento privado, o digest de referência, a origem do módulo, o roster,
a geração, o hold e o estado DRAINED. Sob a segunda trava kernel, persiste
uma tentativa STARTED antes de qualquer conexão. Conserva a trava durante
toda a transação, inclusive rollback, commit, fechamento da conexão e
dispose. Reconfere o hold antes e depois do DDL, sem retirar o preflight
de escritores, o advisory lock, os limites de tempo ou as verificações
de autoridade e revisão do runner original. O código da barreira integra
o digest do runner. O comando check continua exclusivamente leitura, sem
exigir hold ou autorização de operação.

A tentativa termina COMPLETED somente após o retorno normal da execução
transacional; exceção mantém UNCONFIRMED, morte de processo pode deixar
STARTED. Mesmo após terminar, o hold permanece: nenhum replay, release ou
resume automático. Outro executor ou release concorrente é recusado
enquanto a trava está ocupada. Para liberar uma tentativa consumida é
necessária reconciliação explícita pelo operador (`reconciled=True`), após
conferir o estado real, efeitos e autorização; a flag por si não é prova
dessa conferência. Não reutilizar o hold/approval_ref consumido para nova
tentativa. Operação de rollback exige autorização própria.

Esse contrato não resolve a cobertura dos clientes não cooperativos ou
de outro host, não comprova a disponibilidade de todos os ingressos e
não delega autoridade ao ledger local. O compartilhamento de processo com
MAESTRO exige resolução independente antes de uma janela real. Sem
autorização de publicação, instalação ou execução produtiva.
