/** Test and harness data: D1's example blocks (read from the repository) and the prototype's Sonnet-written blocks. */
import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = join(process.cwd(), "..");
const json = (rel: string): unknown => JSON.parse(readFileSync(join(root, rel), "utf8").replace(/^﻿/, "")) as unknown;

/**
 * D1's original example pair (a variance slider and a quiz): obsidian-plugin/test/fixtures/evoui/examples.json, the
 * same blocks src/evowiki/deep/evoui/examples.json held until evoui v1 (3c9e6b2) turned that file into templates.
 */
export const EXAMPLES = json("obsidian-plugin/test/fixtures/evoui/examples.json") as Record<string, unknown>[];
/** obsidian-plugin/test/fixtures/evoui/generated-blocks.json: three blocks a model wrote against the spec. */
export const SONNET = (json("obsidian-plugin/test/fixtures/evoui/generated-blocks.json") as { blocks: Record<string, unknown>[] }).blocks;
/** Every block with its fence text, as Obsidian hands it to the processor. */
export const fence = (block: unknown): string => JSON.stringify(block, null, 1);
