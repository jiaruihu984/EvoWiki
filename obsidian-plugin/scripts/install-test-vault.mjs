import { copyFileSync, mkdirSync } from "node:fs";

const dest = "test-vault/.obsidian/plugins/evowiki";
mkdirSync(dest, { recursive: true });
for (const f of ["main.js", "manifest.json", "styles.css"]) copyFileSync(f, `${dest}/${f}`);
console.log(`installed into ${dest} — open test-vault/ as a vault in Obsidian and enable community plugins`);
