# Investigador V0 — replays sintéticos

Este diretório pertence ao benchmark offline do Investigador Operacional V0, não aos testes conversacionais existentes em `evals/run_local.py`.

**Estado:** fixture e carregador estruturais. Não existe aqui agente autônomo real, inferência de causa, oracle, chamada a Zabbix/Tempo/CI, banco de dados ou execução no AGT.

- `manifest.py` carrega exclusivamente arquivos sintéticos, recusa campos de gabarito e operações de escrita e constrói `SourceEvent` e conectores simulados.
- `synthetic_case_001.json` contém seed inicial separado das respostas que só serão visíveis quando uma ferramenta for consultada. Os timestamps são inteiramente fictícios.
- `tests/test_investigator_replay_fixture_v0.py` confirma a recusa de informações de gabarito, operações mutantes e tenants não sintéticos.

**Não converter aqui incidentes reais diretamente.** Material de Zabbix/CI01 ou CI02, gabarito do avaliador, credenciais e inventários privados permanecem em armazenamento com acesso restrito. A própria issue pública de um incidente pode contaminar a avaliação se o runtime puder acessá-la.

A camada de agente, timeout forte, estado/auditoria durável e orçamento completo serão validados em etapas separadas. O caminho de replay não pretende adivinhar o caminho investigativo nem forçar conclusão. Ver contrato #344, teste cego #342 e comparação CI #345.