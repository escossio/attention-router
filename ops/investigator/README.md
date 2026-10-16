# Investigador real one-shot — candidato offline

`real_once.py` é o entry point de um único ensaio autorizado sobre `evals/investigator_v0/synthetic_case_001.json`. A fixture não inclui oracle. A execução usa somente `SimulatedConnector`; não consulta Zabbix, Tempo, CI ou produção. O agente recebe apenas o catálogo de pares fonte/operação autorizados pelo broker, sem observações antecipadas. A imagem inclui `manifest.py` e a fixture, sem o avaliador estrutural.

Após autorização operacional explícita e instalação das dependências do projeto, o operador projeta **uma chave já existente** em arquivo regular privado `0600`, fora do Git. `OPENAI_API_KEY_FILE` aponta para esse arquivo. `--journal` e `--result` são caminhos absolutos privados fora do Git; o resultado não pode existir antes da execução. Exemplo de formato, sem criar credencial:

```sh
OPENAI_API_KEY_FILE=/caminho/privado/chave \
  python -m ops.investigator.real_once \
  --journal /caminho/privado/investigator.jsonl \
  --result /caminho/privado/resultado.json
```

O parent valida chave, quote e admissão antes de iniciar o child por `run_with_deadline` (75 s, sem shell, ambiente mínimo). O child desabilita o tracing externo do Agents SDK, executa `gpt-5.6-luna` com quatro turns, timeout de 15 s por tentativa, máximo de 500 tokens de saída por tentativa e sem chamadas paralelas ou retry automático. A quote é 4000 tokens de entrada + 2000 de saída, 3200 microUSD segundo a tarifa standard declarada. O gate reserva uma única investigação. Uma reserva não garante teto de cobrança se o provider exceder a quote.

O resultado JSON privado contém `run_id`, `status`, `query_count`, `usage`, `usage_state` (`complete`, `partial` ou `unknown`), `limited` e uma conclusão sanitizada. O hook do SDK retém os tokens de respostas recebidas antes de uma falha posterior; `partial` não é a cobrança total e `unknown` nunca significa custo zero. A conclusão inclui classificação, resumo limitado ou redigido, limitações limitadas e índices/fonte/operação das evidências observadas; não inclui payload, argumentos nem referências brutas. Exit codes: 0 concluído, 2 bloqueado, 3 falhou, 4 limitado, 124 timeout. Ausência de resumo após o limite vira timeout, sem conclusão inferida. O journal opt-in fornece eventos de atividade ao MGMT e não contém prompts, argumentos ou evidências. Tentativas com fonte/operação fora do vocabulário aparecem com evento e ordinal, sem os valores fornecidos pelo modelo. Um `started` isolado aparece como `STARTING`; evento vazio ou stale não é `RUNNING`.

Validação offline focada: `pytest tests/test_investigator_real_once.py`, `python -m py_compile ops/investigator/real_once.py`, `ruff check ops/investigator/real_once.py tests/test_investigator_real_once.py`. O ensaio real, provider e instalação no AGT ficam para um gate operacional separado.
