"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");

const dockerfile = fs.readFileSync("Dockerfile", "utf8");
const deployPs = fs.readFileSync("deploy.ps1", "utf8");
const deploySh = fs.readFileSync("deploy.sh", "utf8");
const provisionPs = fs.readFileSync("provision-share-key.ps1", "utf8");
const provisionSh = fs.readFileSync("provision-share-key.sh", "utf8");
const main = fs.readFileSync("main.py", "utf8");

assert.match(dockerfile, /CMD \["sh", "-c", "exec gunicorn /, "Gunicorn muss Container-Signale direkt erhalten");
assert.match(deployPs, /--env-file \$SecretEnvFile/, "PowerShell-Deployment muss ein geheimes Env-File verwenden");
assert.match(deploySh, /--env-file "\$SECRET_ENV_FILE"/, "POSIX-Deployment muss ein geheimes Env-File verwenden");
assert.doesNotMatch(deployPs, /--env\s+"SHARE_SIGNING_PRIVATE_KEY=\$SigningKey"/, "Privater Seed darf nicht in PowerShell-Prozessargumenten stehen");
assert.doesNotMatch(deploySh, /--env\s+"SHARE_SIGNING_PRIVATE_KEY=\$SIGNING_KEY"/, "Privater Seed darf nicht in POSIX-Prozessargumenten stehen");
assert.match(deployPs, /Protect-SecretDirectory/);
assert.match(deployPs, /\[IO\.File\]::SetAccessControl/);
assert.match(deploySh, /chmod 600 "\$SECRET_ENV_FILE"/);
assert.match(deployPs, /stop --time 45/);
assert.match(deploySh, /stop --time 45/);
assert.match(deployPs, /--stop-timeout 45/);
assert.match(deploySh, /--stop-timeout 45/);

for (const source of [deployPs, deploySh]) {
  assert.match(source, /\.share-public-keyring\.json/, "Deployment muss den persistenten Public-Key-Keyring laden");
  assert.match(source, /MAX_CONCURRENT_INSPECTIONS_PER_WORKER/, "Deployment muss das Inspect-Speicherlimit weiterreichen");
}
assert.match(provisionPs, /Save-CurrentPublicKey/);
assert.match(provisionPs, /AllowInvalidateExistingLinks/);
assert.match(provisionSh, /CURRENT_PUBLIC_KEY/);
assert.match(provisionSh, /allow-invalidate-existing-links/);
assert.match(main, /host=os\.getenv\("HOST", "127\.0\.0\.1"\)/, "Direktstart muss standardmäßig nur an Loopback binden");

console.log("Deployment-, Secret- und Rotationsverträge erfolgreich geprüft.");
