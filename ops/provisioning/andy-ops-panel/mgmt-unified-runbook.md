# Portal MGMT — implantação por etapas

## Inventário verificado

- Collector OTLP emite traces ao Tempo; foram vistos traces recentes do worker e transport.
- Dashboards existentes: ROC-E2E / Operational Flow, Live Message Path e Network / OSI.
- O Dozzle funciona sob a rota-base `/observability/dozzle/` em candidato isolado.
- O GoAccess exige proxy HTTP mais WebSocket; o HTML embute a porta interna 7890.
- A versão de testes do Apache traduziu a porta do HTML para 443 e o handshake retornou HTTP 101.

## Gates

1. Checar os checks obrigatórios do GitHub antes de publicação.
2. Preservar vhost e arquivos atuais para rollback.
3. Testar em loopback os painéis, assets, GoAccess HTML, Dozzle e WS.
4. Manter HTTP Basic no MGMT, incluindo todas as subrotas.
5. Alterar a URL WS de produção para a pública com HTTPS, após backup.
6. Adotar o Apache candidato apenas após sintaxe e autenticação aprovadas.
7. Atualizar o MGMT com as URLs relativas `/observability/goaccess/` e `/observability/dozzle/`.
8. Fazer teste externo por rede móvel antes de declarar concluído.
9. Conservar opção de abrir o Grafana em outra aba quando a sessão impedir iframe.
10. Não marcar iframe como saudável só pelo evento de carregamento.

Nenhum alerta novo é criado nesta frente. Os coletores existentes continuam privados.
