import { expect, it } from "vitest";
import { readProfileFields, writeProfileFields } from "./profile-fields";
it("the form hides metadata and preserves source identities and mastery sections", () => {
  const note = "## About me\n- Nurse\n\n## What I want to learn\n- Statistics\n\n## How I learn best\n<!--evowiki:derived 0123456789abcdef 0123456789abcdef-->\n- Use concrete examples\n\n## What I know\n- Existing mastery\n";
  const fields = readProfileFields(note);
  expect(fields).toEqual({ who: "Nurse", want: "Statistics", how: "Use concrete examples" });
  const changed = writeProfileFields(note, { ...fields, who: "Nurse\nPrior experience with Excel", how: "Use concrete examples\nExplain new terms first" }, "en");
  expect(changed).toContain("<!--evowiki:derived 0123456789abcdef 0123456789abcdef-->\n- Use concrete examples");
  expect(changed).toContain("## What I know\n- Existing mastery"); expect(changed).toContain("## About me\n- Nurse\n- Prior experience with Excel");
});
