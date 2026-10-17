# Candidato operacional unificado MAESTRO / Relay

Estado: CANDIDATO SEM IMPLANTACAO, 2026-10-17.
Base imutavel: feat/maestro-relay-compat-pr333-20261017,
SHA c5d6276b4750405285e3caf3f5cbf28d34fb0f33, PR #369.
Combinado com Relay e HTTPS das PRs #371 e #372. Sem reimplementar motores.

## Motivo
A base PostgreSQL de estagio ja esta na revisao
0058_group_contextual_presence. A antiga PR #368 usava
0058_maestro_relay_receipts em paralelo; nao pode ser aplicada
diretamente sem reconciliar Alembic. A PR #369 ja implementou
a sequencia 0058_group_contextual_presence ->
0059_maestro_relay_receipts e provou preservar dados contextuais.

Esta candidata reaproveita 0059 sem tocar em outras migracoes.
O ingress legado HTTP permanece inalterado em sua superficie,
sem rota MAESTRO; o listener HTTPS privado, opcional, oferece
somente a rota de recibos e health no mesmo processo e container.

## Principios objetivos
- Prioridade: restabelecer o circuito de ocorrencias com prova
  sintetica no ambiente real antes de Zabbix live.
- Rede: reusar endereco e rede atuais do ingresso. Sem nova
  VLAN, firewall completo, mTLS ou DNS nao essenciais.
- Transporte: HTTPS com identidade verificavel, podendo usar
  certificado local com SAN de IP e trust explicitamente
  fornecido ao Relay; nao desativar verificacao TLS.
- Autoridade: Bearer de servico com tenant/binding/escopo,
  ACK duravel no PostgreSQL, idempotencia do Relay.
- Relacionamento com fluxo de investigacao: esta entrega
  comprova recebimento e recibo; NAO cria processamento
  investigativo MAESTRO novo nem executa correcoes.

## Sequencia de ativacao posterior a CI
1. Inventariar consumidores atuais, backup privado
   do Compose, imagem, ambiente e banco.
2. Gerar certificado de stage privado e confiar apenas
   na chave/certificado publico do servidor no cliente.
3. Certificar imagem do SHA exato e preparar Compose com
   mesma VLAN/endereco, health HTTP antigo e porta HTTPS.
4. Fazer cutover controlado do ingresso, verificando ambos
   os health checks. Rollback automatico para a imagem
   antiga se HTTP legado ou HTTPS falhar.
5. Executar migration 0059 em banco preservado, apos
   prova de backup, sem downgrade destrutivo.
6. Provisionar tenant/binding/credencial de stage,
   validar envio sintético e ACK/duplicate/retry.
7. Somente depois configurar Zabbix a emitir eventos
   novos, com saude do Relay e contadores monitorados.

A ausencia de regras novas de firewall nao e bloqueio
desta etapa por decisao operacional do responsavel. Nao
presumir que NAT garante confidencialidade interna ou
autorizacao; token e TLS verificado seguem mantidos.

Live continuity preservada. Historical acceleration
deliberadamente adiada. Nenhum deploy implicito deste commit.
