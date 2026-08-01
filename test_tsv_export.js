const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("static/app.js", "utf8");
const match = source.match(/function tsvCell\(value\) \{[\s\S]*?\n\}/);
assert.ok(match, "tsvCell() wurde in static/app.js nicht gefunden");
const tsvCell = vm.runInNewContext(`(${match[0]})`);

const cases = [
  ["Normal", "Normal"],
  ['=HYPERLINK("http://evil","x")', '"\'=HYPERLINK(""http://evil"",""x"")"'],
  ["+1234567890", "'+1234567890"],
  ["-1", "'-1"],
  ["@SUM(A1)", "'@SUM(A1)"],
  ["A=B", "A=B"],
  ["Wert mit\tTab", '"Wert mit\tTab"'],
  ['=Formel mit "Anführungszeichen"', '"\'=Formel mit ""Anführungszeichen"""'],
  ["", ""],
  [null, ""],
  [undefined, ""],
  ["   =SUM(A1)", "'   =SUM(A1)"],
  ["  -42", "'  -42"],
  ["\uFEFF=SUM(A1)", "'\uFEFF=SUM(A1)"],
  ["\n=SUM(A1)", '"\'\n=SUM(A1)"'],
  ["\tNormal", '"\'\tNormal"'],
  ["\rNormal", '"\'\rNormal"'],
];

for (const [input, expected] of cases) {
  assert.strictEqual(tsvCell(input), expected, `Unerwarteter TSV-Wert für ${String(input)}`);
}

console.log(`${cases.length} TSV-Injection-Testfälle erfolgreich.`);
