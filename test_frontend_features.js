const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const {TextDecoder, TextEncoder} = require("node:util");

const source = fs.readFileSync("static/app.js", "utf8");
const html = fs.readFileSync("templates/index.html", "utf8");
const css = fs.readFileSync("static/app.css", "utf8");

const requiredIds = [
  "data-check", "data-quality-summary", "parse-delimiter", "parse-encoding",
  "parse-decimal", "parse-thousands", "reinspect-button", "preview-table",
  "column-profile", "column-config", "segment-estimate", "bandwidth",
  "share-expiry-days", "toggle-histogram", "toggle-rug", "toggle-reference",
  "reset-zoom", "export-svg", "export-png", "chart-tooltip", "chart-summary",
  "share-password", "unlock-dialog", "unlock-password", "unlock-share",
  "loading-stage", "cancel-analysis"
];

const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
assert.equal(new Set(ids).size, ids.length, "HTML enthÃ¤lt doppelte IDs");
for (const id of requiredIds) {
  assert.ok(ids.includes(id), `Erforderlicher UI-Hook #${id} fehlt`);
}
assert.match(html, /id="data-check"[^>]*hidden/, "Datencheck muss initial verborgen sein");
assert.match(html, /id="chart-toolbar"[^>]*hidden/, "Diagrammwerkzeuge mÃ¼ssen initial verborgen sein");

assert.match(css, /prefers-reduced-motion\s*:\s*reduce/, "Reduced-Motion-Regeln fehlen");
assert.match(css, /focus-visible/, "Sichtbare Tastaturfokus-Regeln fehlen");
assert.match(css, /overflow-x\s*:\s*(?:auto|clip)/, "Horizontaler Ãœberlauf ist nicht begrenzt");

for (const contract of [
  'form.append("upload_token"', 'api("/api/estimate"', 'form.append("segment_top_n"',
  'setAttribute("aria-sort"', 'crypto.subtle.encrypt({name: "AES-GCM"',
  "expiryFromPayload", "exportPng", "exportSvg", "formatStatistic",
  'setAttribute("data-segment-label", label)', "estimateGeneration",
  "state.estimateAbort?.abort()", "resultCoverageText"
]) {
  assert.ok(source.includes(contract), `Frontend-Vertrag fehlt: ${contract}`);
}
assert.match(source, /clone\.append\(legend\)/, "SVG-Export muss eine Segmentlegende enthalten");
assert.match(source, /exportedRoot\.getAttribute\("height"\)/, "PNG-Export muss die LegendenhÃ¶he Ã¼bernehmen");
assert.match(
  source,
  /if \(!alias && !unit\) continue;/,
  "Leere Alias-/Einheitenkonfigurationen dÃ¼rfen bei breiten Dateien nicht gesendet werden"
);

const cryptoStart = source.indexOf("function base64urlFromBytes");
const cryptoEnd = source.indexOf("function requestSharePassword");
assert.ok(cryptoStart >= 0 && cryptoEnd > cryptoStart, "Share-Kryptofunktionen fehlen");
const cryptoFunctions = source.slice(cryptoStart, cryptoEnd);
const context = {
  crypto: webcrypto,
  TextEncoder,
  TextDecoder,
  Uint8Array,
  ArrayBuffer,
  btoa: value => Buffer.from(value, "binary").toString("base64"),
  atob: value => Buffer.from(value, "base64").toString("binary")
};
vm.createContext(context);
vm.runInContext(`
  const ENCRYPTED_SHARE_PREFIX = "enc.";
  const PBKDF2_ITERATIONS = 250000;
  ${cryptoFunctions}
  globalThis.shareCrypto = {encryptCompressed, decryptCompressed};
`, context);

(async () => {
  const plain = new TextEncoder().encode("signiertes Testresultat");
  const encrypted = await context.shareCrypto.encryptCompressed(plain, "sehr-sicheres-testpasswort");
  assert.ok(encrypted.startsWith("enc."), "VerschlÃ¼sselter Link besitzt kein FormatprÃ¤fix");
  const decrypted = await context.shareCrypto.decryptCompressed(encrypted.slice(4), "sehr-sicheres-testpasswort");
  assert.deepEqual(Buffer.from(decrypted), Buffer.from(plain), "AES-GCM-Roundtrip ist fehlerhaft");
  await assert.rejects(
    context.shareCrypto.decryptCompressed(encrypted.slice(4), "falsches-passwort"),
    /Passwort ist falsch|verÃ¤ndert/,
    "Falsches Passwort muss abgewiesen werden"
  );
  console.log(`${requiredIds.length} UI-Hooks und Share-Kryptografie erfolgreich geprÃ¼ft.`);
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
