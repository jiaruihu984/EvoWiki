import { describe, expect, it } from "vitest";
import { readConceptId } from "./mapping";

describe("readConceptId", () => {
  it("reads only evowiki.concept_id and ignores every other key", () => {
    expect(readConceptId({ evowiki: { concept_id: " pca " }, mastery: 1.0, evowiki_mastery: 1 })).toBe("pca");
    expect(readConceptId({ mastery: 1.0, concept_id: "pca" })).toBeNull();
    expect(readConceptId({ evowiki: { concept_id: 3 } })).toBeNull();
    expect(readConceptId({ evowiki: "pca" })).toBeNull();
    expect(readConceptId(undefined)).toBeNull();
  });
});
