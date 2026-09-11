# Andy Integration SDK — Python

Standalone prerelease contract package (`0.1.0a1`). Python 3.11+; CI uses 3.12.
Install from this repository or a built wheel; this extraction does not publish
to PyPI and does not install the Attention Router engine or Pydantic.

```bash
python -m pip install ./sdks/python
python sdks/python/examples/offline.py
```

```python
from andy_integration_sdk import ContractValidationError, parse_contract, serialize_contract

# json_text comes from your integration boundary.
try:
    message = parse_contract(json_text)
    output = serialize_contract(message)
except ContractValidationError as error:
    issues = error.issues  # tuple of ValidationIssue(path, keyword)
```

The public API also exports `validate_contract`, `is_contract`, `get_schema`,
`SCHEMA_ID`, `IntegrationContractMessage`, the five message `TypedDict`s and their
supporting types. The wheel includes `py.typed`. Hints describe structure; runtime
validation enforces formats, patterns, roles and cross-field rules. JSON Schema
integers can be integral Python floats, so numeric hints admit `int | float`.

Validation uses the bundled canonical Draft 2020-12 schema with format checks.
It returns a detached JSON dictionary, without defaults, coercion, normalization
or mutation. Non-JSON native values, cycles, NaN and infinity are rejected. Parsing
uses standard JSON duplicate-key behavior (last value wins); serialization is
ordinary JSON, not canonical signing data. `get_schema()` returns a defensive copy.
Errors expose JSON Pointers and keywords, without rejected values or validator
messages. Paths can contain caller-supplied field names and are not automatically
safe for logs. Issue lists may differ between language validators.

This package makes no network calls. Valid JSON grants no tenant binding, identity,
delivery authorization, execution authority or artifact access. HTTP, signing,
OAuth, retries and uploads remain outside this package.

From the repository root, build and test the installed wheel in a clean virtual
environment with the shared wire corpus and the synthetic offline example, then
check an external typed consumer using the pinned mypy test dependency:

```bash
python scripts/generate_integration_sdks.py --check
python scripts/test_python_integration_sdk.py
```

Installation downloads package dependencies; tests and examples contact no
providers. Schema/types are generated from the canonical contract with
`scripts/generate_integration_sdks.py`; do not edit generated copies.
