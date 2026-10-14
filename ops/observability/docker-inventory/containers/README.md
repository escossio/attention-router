# ROC Docker Inventory — container-first V1

**Propósito:** substituir a coleta via Zabbix Agent2 nativo e o proxy Unix
systemd do AGT por dois serviços completamente empacotados no Docker. O
Zabbix Server, banco, Grafana, Tempo e Flow Map existentes permanecem nos
containers ROC. Nenhum pacote `zabbix-agent2`, PostgreSQL ou Python precisa
ser instalado diretamente no sistema operacional do host.

## Diagrama de autoridade

```text
Zabbix Server ROC (rede andy-roc-monitoring)
        |
        | TCP 10050, exclusivo Docker interno, sem published port
        v
roc-agent2 [UID 1997, cap-drop ALL, rootfs read-only]
        |
        | unix:///run/andy-zabbix-docker/docker.sock
        | Volume Docker compartilhado, somente leitura no Agent2
        v
roc-docker-view [rede none, cap-drop ALL, API GET/HEAD estritamente allowlisted]
        |
        | /var/run/docker.sock, montagem controlada pelo Docker Engine
        v
Docker Engine do host
```

**Limite de segurança:** montagem `docker.sock:ro` NÃO equivale a API
read-only; o daemon Docker atribui autoridade elevada a quem alcança seu
socket. Somente o processo dedicado `roc-docker-view` recebe a montagem; ele
tem `network_mode:none`, não publica portas e rejeita POST/PUT/PATCH/DELETE
com HTTP 405, além de restringir as rotas de leitura. O Agent2 nunca recebe
o socket real. Isolamento lógico não equivale a ausência total de risco de
escalada em caso de vulnerabilidade no proxy.

## Implantação não disruptiva

1. Usar repositório/branch com CI e controles de arquitetura aprovados.
   Instalar apenas Docker Engine e Compose no destino. Declarar a rede
   `andy-roc-monitoring` pelo stack ROC preexistente e iniciar seus serviços.
   Configurar volumes, segredos e integração SNMP/OTel do novo ambiente.
2. Verificar as permissões locais do socket Docker da máquina destino; se
   rootless ou outro caminho, tratar como parâmetro de deploy auditável, não
   inventar socket externo nem publicar a API do Docker via TCP.
3. `docker compose -f ops/observability/docker-inventory/containers/compose.yaml
   build` e `docker compose ... up -d` SOMENTE após confirmar a rede.
4. Ainda sem alterar o host Zabbix, executar a partir do Zabbix Server ROC:
   `zabbix_get -s roc-agent2 -p 10050 -k agent.ping`, e validar as seis
   master keys `andy.docker.inventory*` e a descoberta nativa
   `docker.containers.discovery[false]`. Testar GET permitido, POST 405 e
   rota não permitida 403 no socket intermediário.
5. Fazer backup via **Zabbix API**, não ALTER/UPDATE SQL diretamente, da
   interface principal do host lógico `Andy Engine - AGT01` (interfaceid
   observado 84 no lab; não codificar esse ID). Atualizar exclusivamente sua
   interface para `useip=0, dns=roc-agent2, port=10050`, preservando
   hostid, history, triggers, templates e macros. A aplicação deve resolver
   `roc-agent2` pela rede privada. Se a API exigir algum campo de IP, não
   reutilizar o endereço 10.45.0.2 como dependência funcional.
6. Testar pelo menos dois ciclos de coleta LLD (intervalo mestre 2 min),
   incluindo containers em execução/parados, múltiplas redes, CPU/memória,
   RX/TX, health e estado de observabilidade de cada fonte. Confirmar que
   `UNKNOWN` se recupera com evidência fresca e que o Flow Map permanece
   correto com Tempo indisponível.
7. Só após evidência e confirmação de que não existem outros consumidores
   nativos, desabilitar `andy-zabbix-docker-view.service` e o timer
   `roc-zabbix-agent2-origin.timer`. Não reinstalar `zabbix-agent2` no host.
   A remoção definitiva de antigos serviços é uma etapa separada e auditada.

## Reversão

Antes de atualizar a API, capturar a interface Zabbix e garantir seu backup
privado. Para reverter, restaure a interface validada via API e confira o alvo.
Se o serviço do host foi desinstalado, **não** restaurar para um Agent2
inexistente: reverter o cutover de inventário significa usar outro agente
containerizado certificado ou pausar a coleta com `UNKNOWN`. Não deletar
hosts, itens, trends, history ou templates.

## O que isto NÃO resolve ainda

- O `roc-collector` atual é containerizado, mas usa `network_mode:host`,
  `pid:host` e o socket Docker. O `roc-trace-bridge` também monta esse
  socket. Ambos merecem contratos de autoridade e eliminação de dependências
  específicas do host.
- TCP Brain e o leitor SSH do RouterOS ainda incluem serviços systemd no AGT.
- Volumes Postgres/Grafana/Tempo, identidade de domínio, namespaces de rede,
  políticas VLAN/macvlan, autorização do dispositivo MikroTik e backups
  requerem plano de exportação/migração verificável; containers sozinhos não
  movem os dados persistentes nem o estado da rede física.
- Algumas métricas nativas como temperatura da CPU e filesystem do host
  **não podem ser interpretadas** a partir do filesystem do Agent2 container.
  Exigem exporters dedicados com mounts/sensores explicitamente nomeados e
  privilégios mínimos. O Agent2 desta fatia é **inventário do Docker Engine**
  e não um substituto genérico para sensores físicos do host.

## Evidência do laboratório e testes

O laboratório usa a rede ROC privada já existente, não altera o registro da
interface do host lógico. Após iniciar os dois containers:

```sh
bash ops/observability/docker-inventory/containers/smoke.sh
```

A verificação inclui seis itens mestres de inventário Docker como JSON,
a descoberta nativa via plugin Agent2, `agent.ping` via servidor Zabbix e
as restrições de publicação de portas, permissões e filesystem. Em laboratório
independente, o proxy respondeu GET `/_ping` e `/containers/json` 200,
POST `/containers/create` 405 e GET `/containers/anyone/exec` 403.

Todas as consultas partem dos containers do mesmo projeto (Zabbix Server ou
container efêmero na namespace sem rede). Um novo host físico só precisa
dispor do Docker socket correto: por padrão `/var/run/docker.sock`, com
opção de implantação `ROC_DOCKER_SOCKET_PATH` se diferir. Isso não torna
um Docker Engine indisponível acessível nem suprime controle de autenticação.
