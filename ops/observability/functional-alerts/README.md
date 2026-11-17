# Alertas operacionais Andy — CPU por núcleo e Tempo

## Evidências e desenho

O Zabbix ROC 7.4 é a fonte de eventos. Uma task systemd no AGT01 coleta,
a cada minuto, **todos os núcleos** de AGT01, CI01, CI02 e CI03. CI utiliza
SSH BatchMode somente-leitura para `/proc/stat`. A sonda publica via
`zabbix_sender` do container `andy-roc-agent2`, sem abrir portas públicas,
instalar agentes nos CIs ou montar o socket Docker em outro container.

A regra `OPS_CPU_CORE_HOT_IDLE` detecta 4 ou mais coletas em 5 minutos com
ao menos um núcleo >=90% e média de todos os núcleos <=40%.
Ela não confunde utilização baixa do host com utilização baixa de cada núcleo.
Um item separado marca falhas do coletor e perda de amostras.

O Tempo possui duas regras distintas:
* `OPS_TEMPO_SCHEDULER_EMPTY` (Information): evento repetido de log
  `backendworker ... no jobs found`, que **pode ser fila vazia normal**;
  não é indicador automático de falha de Grafana.
* `OPS_TEMPO_QUERY_UNAVAILABLE` (High): /ready e /api/search sem resposta
  em duas ou mais coletas. Mede comportamento, não apenas texto de log.

## Estado de implantação

* `provision.py` cria itens, hosts de CI, triggers e tags `ops_alert`.
* `action_telegram.py` prepara a ação **desabilitada** (problema +
  recuperação), filtro para `ops_alert`, utilizando mídia Telegram 97
  e destinatário Zabbix Admin. Não habilitar sem o bot e a mídia validados.
* `collect.py` instalado em `/usr/local/lib/andy-ops-alerts`.
  `andy-ops-functional-collector.timer` executa a cada minuto.
* Triggers `[TEST]` podem validar abertura/recuperação sem ocupar CPU.
  O evento de teste deve ficar nitidamente identificado ao usuário.

## Telegram — configuração privada pelo operador

1. Telegram: conversar com @BotFather e enviar /newbot.
   Guardar o token fora do Git e fora do chat público.
2. Abrir conversa privada com o bot e enviar /start.
   Consultar seu chat ID por @myidbot com /getid.
3. Em https://zabbix.escossio.com/ após autenticação:
   Alerts -> Media types -> Telegram: `api_token` com o token,
   `api_parse_mode` igual a `html`; habilitar o tipo de mídia.
4. Users -> Users -> Admin -> Media: criar Telegram, Send to = chat ID;
   Active; gravidade de todas as categorias; disponibilidade 24x7.
5. Confirmar envio do teste direto da mídia. Só depois habilitar
   `ANDY OPS | CPU and Tempo -> Telegram (problem + recovery)`.
6. Reexecutar testes [TEST] com valores 1 e depois 0; comprovar
   no Telegram as mensagens PROBLEMA e RESOLVIDO. Consultar
   `alerts` e `problem` no Zabbix e registrar evidências.
7. Somente depois investigar/corrigir a origem real dos alarmes,
   verificando se o problema específico foi resolvido no Zabbix.

## Comandos de teste controlado (somente com bot pronto)

Usar `docker exec andy-roc-agent2 zabbix_sender -z roc-zabbix-server
-s 'Andy Engine - AGT01' -k andy.alert.test_cpu -o 1` e trocar
`-o 0` para recuperação. Repetir para `andy.alert.test_tempo`.
Nunca injetar valores de teste nos itens de métricas de produção.

## Rollback

`sudo systemctl disable --now andy-ops-functional-collector.timer`
interrompe apenas a coleta. A ação Telegram pode ser desabilitada no Zabbix,
sem afetar outros alertas. Não fazer limpeza destrutiva de eventos,
credenciais, DB, containers ou hosts.

Nenhuma modificação no código, parâmetros ou versão do Grafana ou do Tempo
faz parte desta frente. Os eventos ficam distinguíveis de falhas reais.
