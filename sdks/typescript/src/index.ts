/** Offline integration wire contracts; validation never confers authority. */
import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import schema from "./_schema.json";
import type { IntegrationContractMessage } from "./types.js";
export type * from "./types.js";

export const SCHEMA_ID: string = schema.$id;
const ajv = new Ajv2020({
  strict: true, allErrors: true,
  coerceTypes: false, useDefaults: false, removeAdditional: false,
});
ajv.addKeyword({ keyword: "discriminator", schemaType: "object", valid: true });
addFormats(ajv, { mode: "full" });
const validate = ajv.compile(schema);

export interface ValidationIssue {
  readonly path: string;
  readonly keyword: string;
}

export class ContractValidationError extends Error {
  readonly issues: ReadonlyArray<ValidationIssue>;
  constructor(issues: ValidationIssue[]) {
    super("Invalid integration contract");
    this.name = "ContractValidationError";
    this.issues = Object.freeze(issues.map(issue => Object.freeze({ ...issue })));
  }
}

function jsonError(path: string): never {
  throw new ContractValidationError([{ path, keyword: "json" }]);
}

function pointer(key: string): string {
  return "/" + key.replace(/~/g, "~0").replace(/\//g, "~1");
}

function checkJson(value: unknown, path: string, ancestors: Set<object>): void {
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number" && Number.isFinite(value)) return;
  if (typeof value !== "object" || ancestors.has(value)) jsonError(path);
  const array = Array.isArray(value);
  const prototype = Object.getPrototypeOf(value);
  if (!array && prototype !== Object.prototype && prototype !== null) jsonError(path);
  ancestors.add(value);
  try {
    // Reject accessors, symbols, sparse arrays and values JSON.stringify would silently drop.
    const keys = Reflect.ownKeys(value);
    if (array && keys.length !== value.length + 1) jsonError(path);
    for (const key of keys) {
      if (array && key === "length") continue;
      if (typeof key !== "string") jsonError(path);
      const descriptor = Object.getOwnPropertyDescriptor(value, key)!;
      if (!descriptor.enumerable || !("value" in descriptor)) jsonError(path + pointer(key));
      if (array && !/^(0|[1-9][0-9]*)$/.test(key)) jsonError(path + pointer(key));
      checkJson(descriptor.value, path + pointer(key), ancestors);
    }
  } finally {
    ancestors.delete(value);
  }
}

export function validateIntegrationContract(value: unknown): IntegrationContractMessage {
  let result: unknown;
  try {
    checkJson(value, "", new Set());
    result = JSON.parse(JSON.stringify(value));
  } catch (error) {
    if (error instanceof ContractValidationError) throw error;
    // Do not propagate native errors that might echo caller-supplied data.
    jsonError("");
  }
  if (!validate(result)) {
    throw new ContractValidationError((validate.errors ?? []).map(error => ({
      path: error.instancePath, keyword: error.keyword,
    })));
  }
  return result as IntegrationContractMessage;
}

export function parseIntegrationContract(text: string): IntegrationContractMessage {
  if (typeof text !== "string") jsonError("");
  let value: unknown;
  try { value = JSON.parse(text); } catch { jsonError(""); }
  return validateIntegrationContract(value);
}

export function serializeIntegrationContract(value: unknown): string {
  return JSON.stringify(validateIntegrationContract(value));
}

export function isIntegrationContract(value: unknown): value is IntegrationContractMessage {
  try { validateIntegrationContract(value); return true; }
  catch (error) {
    if (error instanceof ContractValidationError) return false;
    throw error;
  }
}

export function getIntegrationSchema(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(schema));
}
