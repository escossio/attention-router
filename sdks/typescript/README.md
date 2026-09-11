# Andy Integration SDK — TypeScript

Private prerelease contract package (`0.1.0-alpha.1`). Node.js 22+; CI uses Node 22.
This extraction does not publish to npm or depend on the Attention Router engine.

```bash
npm --prefix sdks/typescript ci --ignore-scripts
npm --prefix sdks/typescript run build
cd sdks/typescript
npm pack --ignore-scripts
# In a separate consuming project:
npm install /absolute/path/to/attention-router-integration-sdk-0.1.0-alpha.1.tgz
```

```typescript
import {
  ContractValidationError, parseIntegrationContract, serializeIntegrationContract,
} from "@attention-router/integration-sdk";

// jsonText comes from your integration boundary.
try {
  const message = parseIntegrationContract(jsonText);
  const output = serializeIntegrationContract(message);
} catch (error) {
  if (error instanceof ContractValidationError) {
    const issues = error.issues; // readonly { path, keyword }[]
  } else { throw error; }
}
```

The package supports CommonJS `require` and named ESM imports with generated
TypeScript declarations. It also exports `validateIntegrationContract`,
`isIntegrationContract`, `getIntegrationSchema`, `SCHEMA_ID`,
`IntegrationContractMessage`, the five message interfaces and supporting types.
Static types describe structure; runtime validation enforces semantic constraints.

Ajv uses the bundled canonical Draft 2020-12 schema with full format checks, without
coercion, defaults or unknown-field removal. Validation returns a detached JSON
object. Non-JSON native values, cycles, nonfinite numbers, sparse arrays, accessors
and properties that JSON would silently discard are rejected. Parsing follows
`JSON.parse`, including JavaScript number precision and last-value handling of
duplicate keys. Avoid numbers that cannot be represented exactly in the receiving
language. Serialization is ordinary JSON, not canonical signing data.
`getIntegrationSchema()` returns a defensive copy.

Errors have a generic message and immutable issues containing JSON Pointers and
keywords, without rejected values or raw Ajv messages/params. Paths can contain
caller-supplied field names and are not automatically safe for logs. Issue lists
may differ between language validators.

This package makes no network calls. Valid JSON grants no tenant binding, identity,
delivery authorization, execution authority or artifact access. HTTP, signing,
OAuth, retries and uploads remain outside this package.

```bash
# From the repository root:
python scripts/generate_integration_sdks.py --check
npm --prefix sdks/typescript ci --ignore-scripts
npm --prefix sdks/typescript test
```

Tests build and pack the SDK, install the tarball outside the repository, exercise
the shared wire corpus via the public API, check CJS/ESM imports and declarations,
then compile and run an external TypeScript example. Installation downloads package
dependencies; tests/examples contact no providers. Schema/types are generated with
`scripts/generate_integration_sdks.py`; do not edit generated copies.
