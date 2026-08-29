import fs from "node:fs";

const html = fs.readFileSync("assets/template.html", "utf8");
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) throw new Error("script block missing");
if (/\{\{(?:DATA|MAIN|NAV|NAVQ|STATS|TOTAL)\}\}/.test(html)) {
  throw new Error("legacy data placeholder remains in the authenticated app shell");
}
new Function(match[1]);
console.log("frontend script syntax OK");
