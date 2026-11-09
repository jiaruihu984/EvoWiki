/**
 * Test-only (the contract tests): validates a real response body / SSE payload against the output shape of a pydantic
 * model. The JSON Schema is src/generated/wire-schema.json, emitted from the live models by scripts/gen_plugin_types.py
 * (its `$defs` are keyed by the TS names of src/generated/*.ts: RsiChangeOut, SourceSectionOut, ...). Every field is
 * required and unknown keys are refused, as FastAPI's response_model / model_dump write them. An unknown model name
 * throws (fail closed).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Ajv2020, type ValidateFunction } from "ajv/dist/2020";
import addFormatsModule from "ajv-formats";

export const WIRE_SCHEMA_PATH = join(__dirname, "..", "..", "src", "generated", "wire-schema.json");

interface WireSchema { $id: string; $defs: Record<string, unknown> }

// ajv-formats is CommonJS: under vitest its default import may arrive wrapped once more.
type AddFormats = (ajv: Ajv2020) => Ajv2020;
const addFormats = ((addFormatsModule as unknown as { default?: AddFormats }).default ?? addFormatsModule) as unknown as AddFormats;

export interface WireValidator {
  /** Model names (TS names) the schema defines. */
  readonly models: ReadonlySet<string>;
  /** Error messages with paths (e.g. `PageOut/sections/0/cells/1/band: must be equal to one of the allowed values`); [] = valid. */
  validate(model: string, value: unknown): string[];
}

export function loadWireSchema(path: string = WIRE_SCHEMA_PATH): WireValidator {
  const schema = JSON.parse(readFileSync(path, "utf8")) as WireSchema;
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  addFormats(ajv);
  ajv.addSchema(schema);
  const compiled = new Map<string, ValidateFunction>();
  const models = new Set(Object.keys(schema.$defs));
  return {
    models,
    validate(model, value) {
      if (!models.has(model)) throw new Error(`wire-schema.json has no model ${model} (regenerate: python scripts/gen_plugin_types.py)`);
      let fn = compiled.get(model);
      if (fn === undefined) {
        fn = ajv.compile({ $ref: `${schema.$id}#/$defs/${model}` });
        compiled.set(model, fn);
      }
      if (fn(value)) return [];
      return (fn.errors ?? []).map((e) => {
        const extra = e.keyword === "additionalProperties" ? ` '${String((e.params as { additionalProperty?: string }).additionalProperty)}'`
          : e.keyword === "required" ? ` ('${String((e.params as { missingProperty?: string }).missingProperty)}')` : "";
        return `${model}${e.instancePath}: ${e.message ?? e.keyword}${extra}`;
      });
    },
  };
}
