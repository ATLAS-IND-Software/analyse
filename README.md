# Histo Maker

Histo Maker ist eine datensparsame Webanwendung für KDE-Dichtekurven und deskriptive Statistik aus TSV-, CSV- und TXT-Dateien. Sie bietet einen Datencheck, frei kombinierbare Filter und Segmentierungen, interaktive Diagramme, Exporte sowie kryptografisch signierte Freigabelinks.

Es gibt keine Benutzerkonten und keine Anwendungsdatenbank. Um denselben Datensatz nicht für Datencheck, Schätzung und Analyse mehrfach übertragen zu müssen, hält der Server Uploads jedoch vorübergehend in einem begrenzten RAM-Cache. Die Anwendung ist daher im Betrieb nicht vollständig zustandslos; Details und Datenschutzfolgen stehen unter [Temporärer Uploadcache](#temporärer-uploadcache).

Diese Dokumentation beschreibt Version 3.0.0 und den vorgesehenen Production-Betrieb mit Docker und einem Cloudflare Tunnel auf demselben Host.

## Design-System

Der aus der Oberfläche abgeleitete, projektübergreifende Corporate-Design-Leitfaden steht in [`STYLE_GUIDE.md`](STYLE_GUIDE.md). Wiederverwendbare Farben, Schriften, Abstände, Radien, Schatten und Diagrammfarben sind als CSS Custom Properties in [`design-system/tokens.css`](design-system/tokens.css) definiert.

## Architektur und Datenfluss

```text
Browser
  │ HTTPS
  ▼
Cloudflare Edge
  │ verschlüsselter Tunnel
  ▼
cloudflared auf dem Host
  │ HTTP über Loopback
  ▼
127.0.0.1:8000 → Docker → Gunicorn → Flask
                                      │
                                      └→ flüchtiger RAM-Uploadcache pro Worker
```

Der Docker-Port wird ausschließlich auf `127.0.0.1` veröffentlicht. Unverschlüsselter Origin-Traffic verlässt den Server nicht und ist weder aus dem LAN noch aus dem Internet direkt erreichbar.

Ein typischer Ablauf ist:

1. `/api/inspect` liest und prüft die Datei, liefert Vorschau und Spaltenprofile und legt Rohbytes plus geparsten DataFrame kurzzeitig im RAM ab.
2. Der Browser erhält einen zufälligen Upload-Token. Neue Importregeln können damit auf den bereits übertragenen Rohbytes geprüft werden.
3. `/api/estimate` schätzt nach Filtern und Top-N-Regeln die tatsächliche Segment- und Kurvenzahl.
4. `/api/analyze` verwendet denselben Token, berechnet Kennzahlen und Diagrammdaten und signiert das freigabefähige Ergebnis.
5. Der Browser rendert und exportiert das Ergebnis. Rohzeilen gelangen weder in Exporte der Statistik noch in Freigabelinks.

Ist ein Token abgelaufen oder nicht erreichbar und befindet sich die ursprüngliche Datei noch im Browser, lädt das Frontend sie als Fallback erneut hoch.

## Funktionen

### Datencheck und Importregeln

- TSV, CSV und TXT bis standardmäßig 50 MB
- automatische Erkennung der Zeichenkodierung sowie von Tabulator, Komma, Semikolon oder Pipe
- manuelle Overrides für Encoding, Trennzeichen, Dezimalzeichen und Tausendertrennzeichen
- unterstützte manuelle Encodings: UTF-8, UTF-8 mit BOM, UTF-16, UTF-32, Windows-1252 und ISO-8859-1
- Dezimalpunkt und Dezimalkomma sowie Punkt, Komma, Leerzeichen oder Apostroph als Tausendertrennzeichen
- Vorschau der ersten zehn geparsten Zeilen
- Spaltenprofile mit Typ, fehlenden, ungültigen und nicht-endlichen Werten, Kardinalität, häufigen Werten sowie Min/Max für numerische Spalten
- Qualitätswarnungen für fehlende oder ungültige Werte, `Infinity`, konstante Spalten und hohe Kardinalität
- bessere X-Vorauswahl, bei der ID-artige und nahezu eindeutige monotone Spalten nachrangig behandelt werden
- optionale Anzeigenamen und Einheiten für Spalten; sie erscheinen in Diagramm, Statistik, Export und Reproduzierbarkeitsangaben

Textspalten werden als numerisch übernommen, wenn mindestens 80 % ihrer nicht-leeren Werte mit den effektiven Zahlenregeln lesbar sind. Nicht lesbare Werte einer so erkannten Spalte werden als ungültig gezählt. Der Datencheck zeigt außerdem, ob jede Importregel automatisch erkannt oder manuell gesetzt wurde.

### Filter und Segmentierung

- verschachtelte UND-/ODER-Filter bis acht Ebenen und insgesamt 100 Filterelemente
- bis zu zwei unterschiedliche Segmentspalten
- Kardinalität direkt in der Segmentauswahl und Warnung bei sehr vielen Ausprägungen
- serverseitige Vorschau der gefilterten Zeilen, beobachteten Gruppen, auswertbaren Kurven, Kleingruppen, Ausschlüsse und Freigabesperren über `/api/estimate`
- Top N je Segmentspalte; die gemeinsame Auswahl der Oberfläche gilt für jede gewählte Segmentspalte, während die API auch ein spaltenspezifisches JSON-Objekt akzeptiert. Alle übrigen nicht-leeren Werte werden deterministisch als `Sonstige` zusammengefasst. Existiert bereits ein echter Wert dieses Namens, erzeugt der Server eine kollisionsfreie Bündelbezeichnung und markiert sie strukturiert als Other-Kategorie.
- maximal 80 beobachtete beziehungsweise auswertbare Gruppen pro Analyse

Effektive Gruppen nach Top N mit weniger als zwei endlichen X-Werten werden nicht als Kurve ausgewertet. `plotted_rows` zählt die tatsächlich in auswertbaren Kurven enthaltenen Zeilen; `omitted_small_group_count` und `omitted_small_group_rows` weisen Anzahl und Zeilenumfang der ausgelassenen Gruppen aus. Fehlende Segmentwerte bleiben davon getrennt unter `hue_missing` und werden nicht gruppiert. Top N kann bei zwei Segmentspalten weiterhin ein Produkt aus vielen Kombinationen erzeugen; die Schätzung prüft deshalb die effektive Gruppenzahl nach Filtern und Zusammenfassung.

Nicht-endliche Werte der X-Achse werden ausgeschlossen. Treten `+Infinity` oder `-Infinity` dagegen nur als Segmentwert auf, bleiben beide als getrennte, strukturell markierte Kategorien erhalten und können nicht miteinander oder mit fehlenden Werten kollidieren.

### Statistik und KDE

Die Kennzahlentabelle enthält je Segment:

- Dichtegipfel der KDE und einen getrennten diskreten Modus
- Median, Mittelwert, Q1, Q3, IQR und Median Absolute Deviation (MAD)
- zweiseitiges 95-%-Student-t-Konfidenzintervall des Mittelwerts
- Standardabweichung, Varianz, Spannweite, Minimum und Maximum
- Schiefe, Kurtosis, Umfang und eine heuristische Modalitätsbezeichnung

Ein diskreter Modus wird nur angegeben, wenn die höchste beobachtete Häufigkeit mindestens zwei beträgt und genau ein Wert diese Häufigkeit besitzt. Bei mehreren gleich häufigen wiederholten Modi gilt `mode: null` und `mode_tied: true`; `mode_values` enthält bis zu 20 gebundene Werte, `mode_values_truncated` markiert weitere und `mode_count` enthält ihre gemeinsame Häufigkeit. Der Dichtegipfel ist davon getrennt und entspricht dem Maximum der geschätzten Dichte.

Für die KDE gilt:

- API-seitig Scott, Silverman oder ein positiver numerischer Multiplikator des Scott-Faktors bis 100; `1,0` entspricht Scott. Die Oberfläche bietet 0,25× bis 2,5× und startet bei 1,00×. `kde_bandwidth_factor` meldet je Kurve den tatsächlich wirksamen Faktor.
- 320 Auswertungspunkte pro regulärer KDE
- bei großen Gruppen eine deterministische Stichprobe ohne Zurücklegen von standardmäßig höchstens 20.000 Werten
- exakte Kennzahlen und Histogramme auf allen endlichen Werten, auch wenn die KDE Stichproben verwendet
- Histogrammklassen nach Freedman–Diaconis, ersatzweise Quadratwurzelregel, begrenzt auf 60 Klassen
- deterministische Rug-Stichprobe von standardmäßig höchstens 300 Werten pro Kurve

Die Modalität ist eine bandbreitenabhängige Heuristik. Peaks werden mit 3 % der maximalen Dichte als Prominenzschwelle gesucht; die Anzeige ist kein formaler Test auf Uni-, Bi- oder Multimodalität. Bei singulären oder numerisch problematischen Verteilungen kann die Dichtedarstellung auf Histogrammdaten zurückfallen.

Kurven-, Histogramm- und Rug-Werte werden ohne starre Rundung auf acht Dezimalstellen serialisiert, damit sehr kleine oder sehr große Skalen ihre numerische Auflösung behalten. Nicht stabil darstellbare Extrembereiche werden mit einer verständlichen Fehlermeldung abgewiesen.

Nach dem Filtern projiziert der Server breite Tabellen früh auf X- und ausgewählte Segmentspalten. Verschachtelte Filter werden Kind für Kind ausgewertet und direkt in die bisherige Ergebnismaske kombiniert; auch ein breiter Filterbaum hält dadurch höchstens die akkumulierte Ergebnis- und die aktuelle Kindmaske gleichzeitig. Estimate berechnet Gruppengrößen ohne die vollständigen Analyseserien zu materialisieren. Zusammen mit Stichprobengrenzen und gemeinsamem Semaphor reduziert das den Speicher- und Rechenaufwand großer beziehungsweise breiter Datensätze, ohne die Filter-API zu verändern.

### Ausschlüsse und Reproduzierbarkeit

Vor der Gruppierung entfernt die Analyse fehlende, nicht numerisch lesbare und nicht-endliche X-Werte. Sie weist getrennt aus:

- `x_missing_or_invalid`
- `x_non_finite`
- `x_total`
- `hue_missing`
- `omitted_small_group_rows`

`omitted_small_group_rows` zählt Zeilen in effektiven Gruppen nach Top N mit `n < 2`; `omitted_small_group_count` zählt diese Gruppen und `plotted_rows` die verbleibenden Zeilen in tatsächlich berechneten Kurven. Analyze liefert die beiden Omitted-Felder sowohl auf Ergebnisebene als auch in den Reproduzierbarkeitsangaben; die Zeilenzahl steht zusätzlich unter `exclusions.omitted_small_group_rows`. Estimate liefert dieselben drei Summen und behält `small_group_count` als kompatiblen Namen für die Gruppenanzahl bei.

Das Ergebnis enthält die effektiven Importregeln, Rohspalten für X und Segmentierung, Bandbreite, Top-N-Konfiguration, Alias-/Einheitenkonfiguration, Ausschlüsse und die konfigurierte maximale KDE-Stichprobe. Die Methodik beschreibt außerdem Stichprobenverfahren, Auswertungspunkte, Modalitätsheuristik, MAD und Mittelwert-Konfidenzintervall. Diese Angaben werden im Ergebnisbereich sowie im TSV- und SVG-Export ausgegeben. Im signierten Freigabepayload bleiben Alias-/Einheitenkonfiguration und spaltenbezogene Segmentmetadaten aus Datenschutzgründen auf die X-Spalte und die tatsächlich ausgewählten Segmentspalten beschränkt.

Reproduzierbarkeit setzt denselben Dateninhalt, dieselbe Zeilenreihenfolge, dieselben Import- und Analyseparameter und eine kompatible Anwendungsversion voraus. Die deterministische KDE-Stichprobe macht wiederholte Berechnungen unter diesen Bedingungen stabil; sie ersetzt keine Archivierung der Rohdaten.

### Interaktives Diagramm und Exporte

- Hover-Tooltip mit X-Position und Dichte je sichtbarer Kurve
- ein- und ausblendbare Reihen über die Legende
- farbenblindfreundliche Farben plus unterschiedliche Linienmuster
- zuschaltbares Dichtehistogramm, Rug-Plot sowie Mittelwert- und Medianlinien
- Bereichszoom durch Ziehen im Diagramm und Schaltfläche zum Zurücksetzen
- textliche Diagrammzusammenfassung für zusätzliche Zugänglichkeit
- Datenabdeckung im Ergebniskopf mit dargestellten Zeilen sowie Anzahl und Zeilenumfang ausgelassener Kleingruppen
- sortierbare Statistikspalten mit Tastaturbedienung und `aria-sort`
- responsive Mobilansicht, sichtbare Fokuszustände und Unterstützung von `prefers-reduced-motion`
- Statistik als Zwischenablage oder UTF-8-TSV, Diagramm als SVG oder hochauflösendes PNG

TSV-Ausgaben neutralisieren Formelpräfixe `=`, `+`, `-`, `@`, Tab und Carriage Return vor dem Quoting. Die Reproduzierbarkeitsbereiche von TSV und SVG enthalten unter `Datenabdeckung` ebenfalls dargestellte Zeilen und ausgelassene Kleingruppen. SVG-Exporte hängen außerdem eine umgebrochene Legende aller beim Export sichtbaren Reihen mit deren Farbe und Linienmuster an. Der PNG-Export wird aus genau diesem erweiterten SVG gerendert und enthält dieselbe Legende.

Die Rug-Stichprobe ist nur Teil der lokalen Analyseantwort und kann lokal im Diagramm beziehungsweise dessen visuellem SVG-/PNG-Export erscheinen. Vor dem Signieren eines Freigabelinks entfernt der Server `rug` vollständig aus jeder Kurve.

## Temporärer Uploadcache

Der Uploadcache ermöglicht, denselben bereits übertragenen Datensatz für Datencheck, Schätzung und Analyse zu verwenden. Standardmäßig gelten pro Gunicorn-Worker:

- 600 Sekunden Gültigkeit
- höchstens 256 MB für die Summe der geschätzten Rohdaten- und DataFrame-Größen
- höchstens 100 Einträge
- Verdrängung der am längsten nicht verwendeten Einträge, wenn Byte- oder Eintragslimit erreicht wird

Der Token besteht aus kryptografisch zufälligen URL-sicheren Zeichen und wird zusätzlich an einen Hash der normalisierten Client-IP gebunden. Er ist nur innerhalb des Worker-Prozesses gültig. Ein IP-Wechsel kann einen noch nicht abgelaufenen Token unbrauchbar machen. Abgelaufene Einträge sind nicht mehr abrufbar und werden bei nachfolgenden Cachezugriffen oder Healthchecks bereinigt; ein Prozess- oder Containerneustart verwirft den gesamten Cache.

Datenschutz-Trade-off: Die Datei muss weniger oft über das Netz übertragen werden, bleibt dafür aber bis zur Verdrängung beziehungsweise Bereinigung vorübergehend als Rohbytes und geparster DataFrame im Server-RAM. Es gibt keine persistente Speicherung auf Datenträger oder in einer Datenbank. Das Cachelimit ist kein hartes Prozessspeicherlimit: Parsing, Requests, parallele Analysen und Bibliotheken benötigen zusätzlichen temporären Speicher. Container-RAM, Uploadlimit, Cachegröße und Parallelität müssen gemeinsam dimensioniert werden.

Ist die geschätzte Summe aus Rohbytes und tief gemessener DataFrame-Größe größer als das Cachebudget, weist `/api/inspect` den Datensatz ab. `MAX_UPLOAD_MB` begrenzt nur die Request-/Rohdateigröße und muss daher mit `UPLOAD_CACHE_MAX_MB` abgestimmt werden.

Bei `GUNICORN_WORKERS > 1` besitzt jeder Prozess seinen eigenen Cache. Eine Folgeanfrage kann dann einen anderen Worker erreichen und den Token nicht finden. Für mehrere Worker oder Container werden ein gemeinsamer externer Kurzzeitcache oder garantiertes Session-Stickiness benötigt. Das Standard-Docker-CMD verwendet deshalb einen Worker mit vier Threads.

## Freigabelinks

Ein Freigabelink hat die Form:

```text
https://example.org/#result=<komprimiertes-signiertes-ergebnis>
```

Das URL-Fragment wird bei HTTP-Anfragen nicht an Cloudflare, den Origin oder Access-Logs übertragen. Es enthält aggregierte Diagrammdaten, Kennzahlen, Methodik und Reproduzierbarkeitsangaben, aber weder vollständige Rohzeilen, Dateiname noch die lokalen Rug-Einzelwerte. Der Server erzeugt dafür eine tiefe Kopie des Ergebnisses, entfernt `rug` aus jeder Kurve und reduziert Spaltenkonfiguration sowie spaltenbezogene Segmentmetadaten auf die X-Spalte und die tatsächlich ausgewählten Segmentspalten, bevor er den Payload signiert. Alias- oder Einheitenangaben unbeteiligter Spalten gelangen damit nicht in den Freigabelink. Der Filterausdruck ist standardmäßig ausgeschlossen und wird nur nach ausdrücklicher Auswahl als getrennt signierter, kryptografisch an das Ergebnis gebundener Kontext aufgenommen.

Der Server signiert das Ergebnis mit Ed25519. Beim Öffnen lädt der Browser den passenden öffentlichen Schlüssel über `/api/share-key` und prüft die Signatur lokal. Der Payload enthält auf Wunsch ein signiertes Hinweisdatum für eine lokale Öffnungsfrist; die Oberfläche bietet 1, 7, 30 oder 90 Tage und verwendet standardmäßig 7 Tage. Die API akzeptiert 1 bis 3650 Tage oder kein Hinweisdatum. Die Frist muss vor der Analyse gewählt werden, weil sie Teil des signierten Ergebnisses ist. Nach ihrem Ende verweigert die Anwendung anhand der lokalen Geräteuhr die Anzeige, aber der Inhalt wird weder serverseitig gelöscht noch widerrufen und bleibt im URL-Fragment erhalten.

Die Signatur schützt Integrität und Herkunft, nicht Vertraulichkeit. Ohne optionalen Passwortschutz kann jede Person mit dem Link dessen aggregierten Ergebnisinhalt lesen.

Freigabe ist nur möglich, wenn jede ursprüngliche Segmentgruppe vor einer Top-N-Zusammenfassung mindestens fünf Beobachtungen enthält. Top N kann eine kleine Originalgruppe daher nicht in `Sonstige` verbergen und dadurch freigabefähig machen. Das reduziert Offenlegungsrisiken, garantiert aber keine Anonymität.

### Optionaler Passwortschutz

Der Browser kann den bereits signierten und komprimierten Freigabeinhalt zusätzlich clientseitig verschlüsseln:

- AES-256-GCM
- PBKDF2-HMAC-SHA-256 mit 250.000 Iterationen
- zufälliger Salt mit 16 Byte und zufällige GCM-IV mit 12 Byte
- Mindestpasswortlänge in der Oberfläche: acht Zeichen

Passwort, Klartext und abgeleiteter Schlüssel werden nicht an den Server gesendet. Nach dem Entschlüsseln prüft der Browser weiterhin die Ed25519-Signatur und die lokale Öffnungsfrist.

Das Passwort gehört absichtlich nicht in den Link. Es muss über einen getrennten, sicheren Kommunikationskanal übermittelt werden. Acht Zeichen sind nur die technische Untergrenze; für vertrauliche Ergebnisse ist eine lange, einzigartige Passphrase erforderlich. Wer den verschlüsselten Link besitzt, kann offline Passwörter ausprobieren. Verlorene Passwörter können nicht wiederhergestellt werden.

### Schlüsselring und Rotation

`/api/share-key` veröffentlicht den aktuellen Ed25519-Public-Key und alle konfigurierten historischen Verifikationsschlüssel. `SHARE_PUBLIC_KEYRING` enthält ausschließlich öffentliche Schlüssel und akzeptiert entweder ein JSON-Objekt oder eine Liste:

```json
{"alter-key-id":"<base64url-kodierter-32-byte-public-key>"}
```

```json
[{"key_id":"alter-key-id","public_key":"<base64url-kodierter-32-byte-public-key>"}]
```

Die aktuelle Key-ID wird aus den ersten 16 Hex-Zeichen des SHA-256-Hashs des öffentlichen Schlüssels gebildet. Der aktuelle Schlüssel wird immer automatisch in den ausgelieferten Keyring aufgenommen und gewinnt bei einer ID-Kollision.

Die Provisionierungsskripte pflegen den historischen Keyring standardmäßig automatisch in `.share-public-keyring.json`:

1. Vor Änderung des privaten Seeds fragen sie die laufende Installation unter `http://127.0.0.1:<Port>/api/share-key` ab.
2. Sie validieren die 16-stellige hexadezimale Key-ID und den Base64url-Public-Key mit 32 Byte.
3. Sie führen das Paar atomar mit einem vorhandenen JSON-Keyring zusammen; ältere Einträge bleiben erhalten.
4. Erst danach sichern sie den bisherigen privaten Seed mit UTC-Zeitstempel und erzeugen den neuen Seed.
5. Mit `-Redeploy` beziehungsweise `--deploy` übergeben sie die Keyring-Datei an das Deployment.

Kann der aktuelle Public Key nicht abgerufen oder validiert werden, bricht die Rotation vor der Schlüsseländerung ab. Unter Linux/macOS benötigt dieser sichere Rotationspfad zusätzlich `curl` und `jq`. Historische öffentliche Schlüssel sind nicht geheim, müssen aber gegen versehentliche Änderung geschützt und zusammen mit der Deployment-Konfiguration aufbewahrt werden. Private Seeds und ihre Backups bleiben hochsensibel.

Rotation zunächst prüfen:

```powershell
.\provision-share-key.ps1 -Force -WhatIf
```

```sh
./provision-share-key.sh --force --dry-run
```

Rotation mit automatischer Keyring-Pflege und anschließendem Deployment ausführen:

```powershell
.\provision-share-key.ps1 -Force -Redeploy
```

```sh
./provision-share-key.sh --force --deploy
```

Abweichende private Schlüsseldateien werden bei der Provisionierung mit `-KeyFile <Pfad>` beziehungsweise `SHARE_SIGNING_KEY_FILE=<Pfad>` gewählt; beim PowerShell-Deployment heißt der Parameter `-SigningKeyFile`. Abweichende Keyring-Dateien werden mit `-PublicKeyringFile <Pfad>` beziehungsweise `SHARE_PUBLIC_KEYRING_FILE=<Pfad>` gewählt; beim PowerShell-Deployment heißt der Parameter `-SharePublicKeyringFile`. Nur wenn die bewusste Ungültigkeit bestehender Links akzeptiert wird, lässt sich die Schutzsperre mit `-AllowInvalidateExistingLinks` beziehungsweise `--allow-invalidate-existing-links` übersteuern. Dieser Override überspringt ausschließlich die Aufnahme des aktuell laufenden Public Keys; er ist kein Ersatz für eine fehlende Sicherung.

Die Deploy-Skripte laden `.share-public-keyring.json` automatisch, wenn kein Inline-JSON über `-SharePublicKeyring` beziehungsweise `SHARE_PUBLIC_KEYRING` gesetzt ist. Inline-JSON hat Vorrang. `.gitignore` und `.dockerignore` schließen die Standard-Keyring-Datei aus Repository und Build-Kontext aus.

Unter Windows entfernen Provisionierung und Deployment die ACL-Vererbung der privaten Schlüsseldatei und erlauben Vollzugriff nur dem aktuellen Benutzer, `SYSTEM` und der lokalen Administratorengruppe. Private Backups werden ebenso behandelt. Unter POSIX erhalten private Schlüssel, Backups und die vom Provisionierungsskript geschriebene Keyring-Datei Modus `0600`.

Weitere Grenzen von Freigabelinks:

- maximal etwa 60.000 Zeichen und höchstens 2 MB dekomprimierter JSON-Inhalt im Browser
- serverseitig höchstens `MAX_SHARED_JSON_BYTES` für den UTF-8-Payload oder den unkomprimierten Worst-Case-Envelope; standardmäßig 2.000.000 Byte. Die Variable kann das Limit reduzieren, wird nach oben aber hart auf das Browserlimit von 2.000.000 Byte gedeckelt. Eine zu große Analyse bleibt nutzbar, erhält aber kein Freigabematerial.
- maximal 500 UTF-16-Codeeinheiten für freigaberelevante Spalten-, Segment- und Anzeigenamen sowie Segmentwerte und 10.000 für die optionale Filterzusammenfassung; damit entsprechen Server- und Browserprüfung einander auch bei Zeichen außerhalb der Basisebene
- keine individuelle Widerrufsliste; das signierte Hinweisdatum wird ausschließlich als lokale Öffnungsfrist anhand der Geräteuhr geprüft und entfernt den Inhalt nicht aus dem Fragment
- die Installation und der passende aktuelle oder historische Public Key müssen zum Prüfen erreichbar sein
- moderne Browser mit Web Crypto für Ed25519, PBKDF2 und AES-GCM sowie Compression-/DecompressionStream sind erforderlich
- Links können in Zwischenablage, Browserverlauf, Browser-Synchronisation oder Chatverläufen verbleiben

## Datenschutz und Betriebsmetadaten

- Es gibt keine Benutzerkonten und keine Anwendungsdatenbank.
- Uploads werden nur im Prozess-RAM verarbeitet und kurzzeitig gecacht; sie werden nicht persistent gespeichert.
- Vollständige Rohzeilen und lokale Rug-Einzelwerte werden weder in Freigabelinks noch im Service-Worker-Cache gespeichert.
- Upload-Token und Rate-Limit-Identitäten sind flüchtig; Clientadressen werden vor ihrer Verwendung als Identität gehasht.
- API-, Healthcheck- und beliebige sonstige GET-Antworten werden vom Service Worker nicht gecacht.
- `/health` veröffentlicht nur aggregierte prozesslokale Cache- und Analysemetadaten, keine Token, Dateinamen oder Rohdaten.

Gunicorn schreibt Access-Logs nach stdout. Docker, Cloudflare und das Hostsystem können Betriebsmetadaten wie Zeitpunkt, IP-Adresse oder Pfad entsprechend ihrer jeweiligen Logging-Konfiguration speichern. Für Production müssen Logrotation, Zugriffsschutz und Aufbewahrungsfrist zum geltenden Datenschutzkonzept passen. URL-Fragmente mit Freigabeinhalten erscheinen nicht im HTTP-Access-Log.

## Voraussetzungen

Production:

- Docker Engine oder Docker Desktop
- ein auf demselben Host laufender Cloudflare Tunnel und eine Domain mit aktivem HTTPS
- PowerShell 5.1+ unter Windows oder eine POSIX-Shell mit OpenSSL unter Linux/macOS
- für sichere Rotationen unter Linux/macOS zusätzlich `curl` und `jq`
- gesicherter Speicher für privaten Signierschlüssel, Backups, historischen Public-Key-Keyring und Deployment-Konfiguration

Für lokale Entwicklung zusätzlich:

- Python 3.12+
- Node.js für die JavaScript-Regressionstests

## Production-Schnellstart

Alle folgenden Befehle werden im Verzeichnis `webserver` ausgeführt.

### 1. Signierschlüssel provisionieren

Windows:

```powershell
.\provision-share-key.ps1
```

Linux/macOS:

```sh
chmod +x provision-share-key.sh deploy.sh
./provision-share-key.sh
```

Die Skripte erzeugen einen kryptografisch zufälligen, Base64url-kodierten Ed25519-Seed mit 32 Byte und speichern ihn standardmäßig als `.share-signing-key`. Der Schlüsselwert wird nicht auf der Konsole ausgegeben.

Vor dem ersten Deployment:

1. Zugriff auf die Schlüsseldatei auf das Betriebskonto beschränken.
2. Eine verschlüsselte Sicherung anlegen.
3. Sicherstellen, dass `.share-signing-key*` und `.share-public-keyring.json` weder in Git noch in Docker-Build-Kontexte gelangen.

Die bereitgestellten `.gitignore`- und `.dockerignore`-Dateien schließen aktive Schlüssel, Backups, temporäre Schlüsseldateien und den Standard-Public-Key-Keyring aus.

### 2. Container deployen

Windows:

```powershell
.\deploy.ps1
```

Linux/macOS:

```sh
./deploy.sh
```

Standardmäßig ist die Anwendung anschließend ausschließlich unter `http://127.0.0.1:8000` erreichbar.

### 3. Cloudflare Tunnel konfigurieren

Der Public Hostname des Tunnels muss auf `http://localhost:8000` zeigen.

Empfohlene Cloudflare-Einstellungen:

- HTTPS erzwingen
- TLS-Modus und Zertifikatsverwaltung durch Cloudflare
- optional Cloudflare Access für einen geschlossenen Benutzerkreis
- keine Cache-Regel für `/api/*`, `/health` oder `/service-worker.js`
- Uploadlimit mindestens auf das Anwendungsmaximum abstimmen
- optional eine zusätzliche äußere Rate-Limit-Regel

Die Deploy-Skripte setzen `TRUST_CF_CONNECTING_IP=1`. Das ist nur sicher, weil der Origin an Loopback gebunden ist. Wird der Container später auf einer extern erreichbaren Adresse veröffentlicht, muss diese Einstellung deaktiviert oder der Proxy explizit abgesichert werden.

### 4. Deployment prüfen

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

oder:

```sh
curl --fail http://127.0.0.1:8000/health
```

Die Antwort enthält neben Status und Version prozesslokale Betriebsmetadaten:

```json
{
  "status": "ok",
  "version": "3.0.0",
  "uptime_seconds": 42,
  "upload_cache": {
    "entries": 0,
    "bytes": 0,
    "max_bytes": 268435456,
    "max_items": 100,
    "ttl_seconds": 600
  },
  "analysis": {
    "completed": 0,
    "failed": 0,
    "last_ms": null,
    "average_ms": null,
    "max_concurrent_per_worker": 2,
    "kde_max_sample_size": 20000,
    "max_shared_json_bytes": 2000000,
    "state_scope": "process_local"
  },
  "inspection": {
    "max_concurrent_per_worker": 1
  }
}
```

Anschließend den öffentlichen HTTPS-Hostnamen im Browser öffnen und die Smoke-Tests aus der Releasecheckliste durchführen.

## Failsafe Deployment und Container-Härtung

`deploy.ps1` und `deploy.sh` verwenden folgenden Ablauf:

1. Neues Image vollständig bauen, während der bestehende Container weiterläuft.
2. Bestehenden Container mit bis zu 45 Sekunden Grace-Period stoppen und unter `<name>-rollback` sichern.
3. Neuen Container als `<name>-candidate` auf dem Loopback-Port starten.
4. Bis zu 90 Sekunden auf den Docker-Healthcheck warten.
5. Gesunden Kandidaten auf den Produktionsnamen umbenennen.
6. Restart-Policy `unless-stopped` setzen.
7. Rollback-Container erst nach erfolgreicher Übernahme entfernen.

Schlägt Start oder Healthcheck fehl, wird der Kandidat entfernt und der vorherige Container automatisch zurückbenannt und bei vorherigem Laufzustand neu gestartet. Während des Portwechsels kann es kurzzeitig zu einer Unterbrechung kommen.

Bleibt nach Stromausfall oder Prozessabbruch nur `<name>-rollback` zurück, stellt der nächste Deployment-Aufruf diesen Container wieder her und beendet sich. Danach das Deployment erneut starten.

Den privaten Signierseed übergeben die Deploy-Skripte nicht als Wert in der Docker-Prozessargumentliste. Unmittelbar vor `docker run` erzeugen sie stattdessen eine temporäre `--env-file`: Unter Windows liegen Verzeichnis und Datei unter einer nicht vererbten DACL für den aktuellen Benutzer, `SYSTEM` und Administratoren; unter POSIX erhält die Datei Modus `0600`. Direkt nach Rückkehr von `docker run` wird sie auch im Fehlerpfad entfernt und die lokale Schlüsselvariable geleert. Der Seed ist danach weiterhin Teil der Containerumgebung; Zugriff auf Docker-Daemon, Containerinspektion und Hostadministration ist deshalb wie Schlüsselzugriff zu behandeln.

Die mitgelieferten Skripte starten den Container zusätzlich mit:

- standardmäßig 1 GB RAM und 2 CPUs
- festem PID-Limit von 256
- Stop-Timeout von 45 Sekunden; das Docker-CMD startet Gunicorn per `exec`, damit Terminierungssignale den Serverprozess direkt erreichen
- schreibgeschütztem Root-Dateisystem
- `/tmp` als 64-MB-`tmpfs` mit `noexec` und `nosuid`
- entfernten Linux-Capabilities und `no-new-privileges`
- nicht privilegiertem Containerbenutzer mit UID 10001

Manuelle Zustandsprüfung:

```sh
docker ps -a --filter "name=histo-maker"
docker inspect --format '{{.State.Health.Status}}' histo-maker
docker logs --tail 100 histo-maker
```

## Deployment-Konfiguration

Von den mitgelieferten Deployment-Skripten unterstützte Einstellungen:

| Zweck | PowerShell-Parameter | Shell-Variable | Standard |
|---|---|---|---:|
| Host-Port auf Loopback | `-Port` | `PORT` | `8000` |
| Containername | `-ContainerName` | `CONTAINER_NAME` | `histo-maker` |
| Imagename | `-ImageName` | `IMAGE_NAME` | `histo-maker:latest` |
| maximales Uploadvolumen | `-MaxUploadMb` | `MAX_UPLOAD_MB` | `50` MB |
| Inspect-Limit | `-InspectRateLimit` | `INSPECT_RATE_LIMIT` | `30` |
| Analyse-Limit | `-AnalyzeRateLimit` | `ANALYZE_RATE_LIMIT` | `10` |
| Estimate-Limit | `-EstimateRateLimit` | `ESTIMATE_RATE_LIMIT` | `30` |
| parallele Estimate-/Analyseanforderungen | `-MaxConcurrentAnalysesPerWorker` | `MAX_CONCURRENT_ANALYSES_PER_WORKER` | `2` |
| parallele Dateiinspektionen | `-MaxConcurrentInspectionsPerWorker` | `MAX_CONCURRENT_INSPECTIONS_PER_WORKER` | `1` |
| Uploadcache-TTL | `-UploadCacheTtlSeconds` | `UPLOAD_CACHE_TTL_SECONDS` | `600` s |
| Uploadcache-Größe | `-UploadCacheMaxMb` | `UPLOAD_CACHE_MAX_MB` | `256` MB |
| Uploadcache-Einträge | `-UploadCacheMaxItems` | `UPLOAD_CACHE_MAX_ITEMS` | `100` |
| maximale KDE-Stichprobe | `-KdeMaxSampleSize` | `KDE_MAX_SAMPLE_SIZE` | `20000` |
| maximale Rug-Stichprobe | `-RugMaxPoints` | `RUG_MAX_POINTS` | `300` |
| Container-RAM | `-MemoryLimit` | `MEMORY_LIMIT` | `1g` |
| Container-CPU | `-CpuLimit` | `CPU_LIMIT` | `2.0` |
| historische Public Keys | `-SharePublicKeyring` | `SHARE_PUBLIC_KEYRING` | leer |
| historische Public-Key-Datei | `-SharePublicKeyringFile` | `SHARE_PUBLIC_KEYRING_FILE` | `.share-public-keyring.json` |
| Signierschlüsseldatei | `-SigningKeyFile` | `SHARE_SIGNING_KEY_FILE` | `.share-signing-key` |

PowerShell-Beispiel:

```powershell
.\deploy.ps1 `
  -Port 8080 `
  -ImageName histo-maker:3.0.0 `
  -MaxUploadMb 50 `
  -EstimateRateLimit 30 `
  -MaxConcurrentInspectionsPerWorker 1 `
  -MaxConcurrentAnalysesPerWorker 2 `
  -UploadCacheTtlSeconds 600 `
  -UploadCacheMaxMb 256 `
  -KdeMaxSampleSize 20000 `
  -MemoryLimit 1g `
  -CpuLimit 2.0 `
  -SharePublicKeyringFile C:\Secrets\histo-maker-public-keyring.json `
  -SigningKeyFile C:\Secrets\histo-maker-signing-key
```

Linux/macOS-Beispiel:

```sh
PORT=8080 \
IMAGE_NAME=histo-maker:3.0.0 \
MAX_UPLOAD_MB=50 \
ESTIMATE_RATE_LIMIT=30 \
MAX_CONCURRENT_INSPECTIONS_PER_WORKER=1 \
MAX_CONCURRENT_ANALYSES_PER_WORKER=2 \
UPLOAD_CACHE_TTL_SECONDS=600 \
UPLOAD_CACHE_MAX_MB=256 \
KDE_MAX_SAMPLE_SIZE=20000 \
MEMORY_LIMIT=1g \
CPU_LIMIT=2.0 \
SHARE_PUBLIC_KEYRING_FILE=/srv/secrets/histo-maker-public-keyring.json \
SHARE_SIGNING_KEY_FILE=/srv/secrets/histo-maker-signing-key \
./deploy.sh
```

`SHARE_PUBLIC_KEYRING` enthält JSON und sollte in der Shell korrekt gequotet oder aus einer geschützten Deployment-Konfiguration gesetzt werden. Das PowerShell-Skript übernimmt standardmäßig `$env:SHARE_PUBLIC_KEYRING` in `-SharePublicKeyring`. Ist kein Inline-Wert vorhanden, lesen beide Skripte die mit `-SharePublicKeyringFile` beziehungsweise `SHARE_PUBLIC_KEYRING_FILE` gewählte Datei.

### Laufzeitvariablen

Vom Python-Prozess beziehungsweise Docker-CMD unterstützte Variablen:

| Variable | Standard | Bedeutung |
|---|---:|---|
| `APP_VERSION` | Inhalt von `VERSION` | Anwendungsversion |
| `PORT` | `8000` | interner Gunicorn-/Flask-Port |
| `HOST` | `127.0.0.1` | Bind-Adresse ausschließlich beim direkten `python main.py`-Start |
| `MAX_UPLOAD_MB` | `50` | maximale Request-/Uploadgröße |
| `SHARE_SIGNING_PRIVATE_KEY` | – | Base64url-kodierter Ed25519-Seed mit 32 Byte |
| `SHARE_SIGNING_KEY_FILE` | `.share-signing-key` | alternative Schlüsseldatei beim direkten Start |
| `SHARE_PUBLIC_KEYRING` | leer | historische Ed25519-Public-Keys als JSON |
| `TRUST_CF_CONNECTING_IP` | `0` | `CF-Connecting-IP` als Clientadresse verwenden |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Länge des Sliding-Window-Limits |
| `RATE_LIMIT_INSPECT_PER_WINDOW` | `30` | Dateiinspektionen pro Client und Fenster |
| `RATE_LIMIT_ESTIMATE_PER_WINDOW` | `30` | Segmentschätzungen pro Client und Fenster |
| `RATE_LIMIT_ANALYZE_PER_WINDOW` | `10` | Analysen pro Client und Fenster |
| `RATE_LIMIT_MAX_CLIENTS` | `10000` | maximale flüchtige Client-Buckets |
| `MAX_CONCURRENT_ANALYSES_PER_WORKER` | `2` | gemeinsame gleichzeitige Estimate-/Analyseanforderungen pro Worker |
| `MAX_CONCURRENT_INSPECTIONS_PER_WORKER` | `1` | gleichzeitige Dateiinspektionen pro Worker |
| `UPLOAD_CACHE_TTL_SECONDS` | `600` | Zugriffsgültigkeit temporärer Uploads |
| `UPLOAD_CACHE_MAX_MB` | `256` | Cachebudget für Rohbytes plus DataFrames pro Worker |
| `UPLOAD_CACHE_MAX_ITEMS` | `100` | maximale Cacheeinträge pro Worker |
| `KDE_MAX_SAMPLE_SIZE` | `20000` | maximale Werte je KDE-Berechnung |
| `RUG_MAX_POINTS` | `300` | maximale Rug-Werte je Kurve |
| `MAX_SHARED_JSON_BYTES` | `2000000` | serverseitiges Byte-Limit für signierten Payload beziehungsweise Worst-Case-Envelope; nach oben auf 2.000.000 gedeckelt |
| `MAX_FORM_JSON_CHARS` | `100000` | Größenlimit für Filter- und Spaltenkonfigurations-JSON |
| `MAX_COLUMN_CONFIG_ITEMS` | `500` | maximale Alias-/Einheiten-Einträge |
| `LOG_LEVEL` | `INFO` | Python-Loglevel |
| `GUNICORN_WORKERS` | `1` | Anzahl Gunicorn-Prozesse im Docker-CMD |
| `FLASK_DEBUG` | `0` | Debugmodus nur beim direkten `python main.py`-Start |

`HOST` wirkt nicht auf den Docker-/Gunicorn-Start. Gunicorn bindet im Container weiterhin an `0.0.0.0:$PORT`; das Deployment veröffentlicht diesen Containerport ausschließlich über `127.0.0.1:<Host-Port>` auf dem Host.

Aus Kompatibilitätsgründen akzeptiert der Server für den historischen Schlüsselring auch `SHARE_HISTORICAL_PUBLIC_KEYS`, `SHARE_VERIFICATION_PUBLIC_KEYS` oder `SHARE_PUBLIC_KEYS`; neue Deployments sollten ausschließlich `SHARE_PUBLIC_KEYRING` verwenden.

Die Deploy-Skripte reichen Anwendungsversion, privaten Schlüssel, Public-Key-Keyring, Cloudflare-Vertrauen, Uploadlimit, drei Ratenlimits, Inspect- und Analyseparallelität, Cachekonfiguration sowie KDE- und Rug-Grenzen an den Container weiter. `RATE_LIMIT_WINDOW_SECONDS`, `RATE_LIMIT_MAX_CLIENTS`, `MAX_SHARED_JSON_BYTES`, `MAX_FORM_JSON_CHARS`, `MAX_COLUMN_CONFIG_ITEMS`, `LOG_LEVEL` und `GUNICORN_WORKERS` werden nicht automatisch aus der Host-Umgebung übernommen und benötigen bei Abweichungen eine eigene Orchestrierungsdefinition oder zusätzliche kontrollierte `--env`-Optionen.

## Rate Limiting, Kapazität und Health-Metadaten

Die Anwendung führt ein Thread-sicheres Sliding-Window-Limit im Prozessspeicher. Clientadressen werden normalisiert, gehasht und nicht dauerhaft gespeichert. Bei Überschreitung antwortet die API mit `429 Too Many Requests` und `Retry-After`.

Sind alle gemeinsamen Estimate-/Analyse-Slots belegt, antworten `/api/estimate` und `/api/analyze` mit `503 Service Unavailable` und `Retry-After: 5`. Estimate berechnet keine KDE, nutzt aber denselben Semaphor zur Begrenzung paralleler speicherintensiver Datenvorbereitung und besitzt zusätzlich ein eigenes Ratenlimit.

Dateiinspektionen verwenden einen separaten prozesslokalen Semaphor mit standardmäßig einem Slot. Ist er belegt, antwortet `/api/inspect` ebenfalls mit `503 Service Unavailable` und `Retry-After: 5`. So werden paralleles Parsen, Profiling und Einfügen in den RAM-Cache unabhängig von Estimate und Analyse begrenzt.

Gunicorn startet standardmäßig einen Worker mit vier Threads. Rate Limits, Uploadcache, Inspect-/Analyse-Semaphoren und Health-Metriken gelten pro Prozess. Bei mehreren Workern oder Containern sind gemeinsame externe Begrenzung und Cachekoordination erforderlich. Der antwortende Worker liefert in `/health`:

- Laufzeit in Sekunden
- aktuelle Cacheeinträge und Cachebytes sowie konfigurierte Grenzen und TTL
- Anzahl erfolgreicher und fehlgeschlagener Analysen
- letzte und durchschnittliche Dauer erfolgreicher Analysen
- getrennte Inspect- und Estimate-/Analyse-Parallelitätsgrenzen sowie KDE-Stichproben- und Share-Größenlimit
- `state_scope: process_local` als ausdrücklichen Gültigkeitsbereich

## API

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/` | Anwendung |
| `GET` | `/health` | Healthcheck plus prozesslokale Cache-/Analysemetadaten |
| `GET` | `/api/version` | aktuelle Anwendungsversion, `no-store` |
| `GET` | `/api/share-key` | aktueller und historischer Ed25519-Public-Key-Keyring, `no-store` |
| `GET` | `/service-worker.js` | versionsgebundener Service Worker, `no-store` |
| `POST` | `/api/inspect` | Datei prüfen, Importregeln anwenden, Vorschau/Profile und Upload-Token liefern |
| `POST` | `/api/estimate` | effektive Segment- und Kurvenzahl nach Filtern und Top N schätzen |
| `POST` | `/api/analyze` | KDE, Histogramm, Rug, Statistik, Methodik und signiertes Ergebnis berechnen |

`/api/inspect` erwartet eine Datei oder einen gültigen `upload_token`; Importfelder sind `encoding`, `delimiter`, `decimal` und `thousands`. `/api/estimate` und `/api/analyze` akzeptieren ebenfalls Token oder Datei sowie `x_column`, `hue1`, `hue2`, `filter_tree` und `segment_top_n`. Top N kann als einheitliche ganze Zahl oder als JSON-Objekt aus Segmentspalte und Ganzzahl übermittelt werden. `/api/analyze` verarbeitet zusätzlich `bandwidth`, `column_config` und `share_expiry_days`.

Die Estimate-Antwort enthält bis zu 100 Gruppengrößen und markiert eine gekürzte Liste mit `group_sizes_truncated`. Sie enthält außerdem Kardinalitäten vor und nach Top N, kollisionsfreie Other-Bezeichnungen, Ausschlüsse, `plotted_rows`, `omitted_small_group_count`, `omitted_small_group_rows`, den kompatiblen Alias `small_group_count`, Anzahl und Mindestgröße ursprünglicher Gruppen, Freigabesperren und `exceeds_curve_limit`. Die Omitted-Felder beziehen sich auf effektive Gruppen nach Top N mit `n < 2`; fehlende Segmentwerte werden separat unter `exclusions.hue_missing` gezählt.

Die Analyze-Antwort führt `plotted_rows`, `omitted_small_group_count` und `omitted_small_group_rows` auf Ergebnisebene. `exclusions.omitted_small_group_rows` ordnet die ausgelassenen Zeilen den Ausschlüssen zu; beide Omitted-Felder werden außerdem für die Reproduzierbarkeit mitgeführt.

## Offline-Cache

Der Service Worker verwendet eine versionsgebundene Cache-Allowlist:

- `/`
- `app.css`
- `app.js`
- Icon
- Web-App-Manifest

Navigationen arbeiten Network-first und verwenden die App-Shell nur als Offline-Fallback. `/api/*`, `/health`, `/service-worker.js` und nicht explizit freigegebene Ressourcen werden nicht gespeichert. Eine neue Version in `VERSION` erzeugt einen neuen Cache-Namen und entfernt alte App-Caches bei Aktivierung.

## Lokale Entwicklung

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

Linux/macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

Danach `http://127.0.0.1:8000` öffnen. `python main.py` bindet standardmäßig nur an `127.0.0.1`; eine andere Adresse kann bewusst über `HOST` gesetzt werden. Beim direkten lokalen Start wird ohne explizite Konfiguration einmalig `.share-signing-key` erzeugt. Diese Automatik ist für Entwicklung gedacht; Production sollte den Schlüssel bewusst provisionieren und sichern.

## Tests

Backend-, API- und Sicherheitsregressionen:

```powershell
python -m unittest -v test_share.py
```

TSV-/Formula-Injection-Regression:

```powershell
node test_tsv_export.js
```

Frontend-Verträge und Share-Kryptografie:

```powershell
node test_frontend_features.js
```

Die Tests decken unter anderem ab:

- Ed25519-Signatur, Ergebnis-/Filterbindung, Manipulationserkennung und Kleingruppensperre
- signiertes Hinweisdatum für die lokale Öffnungsfrist, Reproduzierbarkeitsdaten, auf X und gewählte Segmentspalten minimierte Alias-/Einheitenangaben und historischen Public-Key-Keyring
- Entfernung lokaler Rug-Einzelwerte aus der signierten Freigabekopie
- Datencheck, Dezimalkomma, manuelle Importregeln, Tausendertrennzeichen und einspaltige Dateien
- Wiederverwendung und Clientbindung des Upload-Tokens
- Estimate, Top N, kollisionsfreie Other-Kategorien, ausgelassene effektive Gruppen/Zeilen und ursprüngliche Kleingruppensperre im Abgleich mit der Analyse
- kontinuierliche Daten ohne falschen diskreten Modus, Dichtegipfel, Quartile, IQR, MAD und 95-%-Mittelwert-KI
- gebundene wiederholte Modi ohne willkürliche Auswahl eines einzelnen Werts
- getrennte kanonische Segmentwerte für `+Infinity` und `-Infinity`
- Ausschluss von `NaN` und `Infinity`, Histogramm, Rug und Bandbreite
- deterministische KDE-Stichprobe bei weiterhin exakten Kennzahlen
- Scott-relative Bandbreitenmultiplikatoren und Kurvenpräzision bei sehr kleinen und großen Skalen
- speicherschonende Kombination breiter Filterbäume, doppelte Segmentspalten, ungültige Bandbreite, Rate Limits sowie getrennte Inspect- und Analyseparallelität
- Health-Metadaten einschließlich Inspect-Grenze, Security-Header und Service-Worker-Cache-Allowlist
- CSV-/Formula-Injection im TSV-Export
- 27 erforderliche UI-Hooks, Accessibility-/Responsive-CSS-Verträge, eingebettete SVG-Exportlegende und AES-GCM/PBKDF2-Roundtrip einschließlich falschem Passwort

Vor jedem Release müssen alle drei Testläufe erfolgreich sein.

## Release-Prozess und Checkliste

1. Alle Python- und Node-Tests ausführen.
2. Version in `VERSION` gemäß Semantic Versioning erhöhen und `CHANGELOG.md` aktualisieren.
3. UTF-8, lokale Markdown-Links und keine versehentlich eingecheckten Secrets prüfen.
4. Aktiven Signierschlüssel und `.share-public-keyring.json` sichern; bei Rotation den automatischen Abruf des bisherigen Public Keys erfolgreich abschließen lassen.
5. Cache-, Upload-, Parallelitäts-, RAM-, CPU- und PID-Limits für das erwartete Datenvolumen prüfen.
6. Image über das Deployment-Skript bauen und ausrollen.
7. Lokalen und öffentlichen `/health` prüfen, einschließlich Version, Cachegrenzen und `state_scope`.
8. Einen Datencheck mit Dezimalkomma und manuellen Importregeln durchführen.
9. Estimate, Top N/`Sonstige`, Filter und Analyse mit Ausschlussanzeige prüfen.
10. Tooltip, Legende, Histogramm, Rug, Referenzlinien, Zoom sowie TSV-, SVG- und PNG-Export testen.
11. Einen normalen und einen passwortgeschützten Freigabelink öffnen; lokale Öffnungsfrist und einen bekannten historischen Schlüssel testen.
12. Sicherstellen, dass das Linkpasswort getrennt vom Link übermittelt wird.
13. Mobilansicht, Tastaturbedienung, Statusmeldungen, Containerstatus und Logs prüfen.
14. Release committen, taggen und dokumentieren.

Die Version wird im UI, in `/api/version`, in `/health`, im Docker-Image und im PWA-Cache verwendet. Eine Versionsänderung ist deshalb auch bei Frontend- oder Cache-relevanten Sicherheitsfixes erforderlich.

## Backup und Wiederherstellung

Zu sichern sind:

- aktiver privater Signierschlüssel
- befristet benötigte private Rotationsbackups
- historischer Public-Key-Keyring
- Deployment- und Ressourcenlimit-Konfiguration
- Cloudflare-Tunnel-Konfiguration und Zugangsdaten außerhalb dieses Projekts

Der Uploadcache wird bewusst nicht gesichert. Zur Wiederherstellung Repository beziehungsweise Release bereitstellen, privaten Schlüssel und Keyring zurückspielen, Dateirechte prüfen, deployen und anschließend `/health`, einen bekannten alten sowie einen neuen Freigabelink prüfen.

Ohne den ursprünglichen privaten Schlüssel funktionieren neue Analysen mit einem neuen Schlüssel weiterhin. Bestehende Links können nur geprüft werden, wenn ihr historischer öffentlicher Schlüssel unter der ursprünglichen Key-ID im Keyring erhalten bleibt.

## Bekannte Grenzen

- Uploadcache, Rate Limits, Inspect-/Analyse-Semaphoren, Analysemetriken und Health-Werte sind pro Worker und nicht zwischen Prozessen oder Containern geteilt.
- Ohne Session-Stickiness kann ein Upload-Token bei mehreren Workern den falschen Prozess erreichen; das Standarddeployment verwendet daher einen Worker.
- Der RAM-Cache ist begrenzt, aber Parsing und Analysen können den darüber hinaus verfügbaren Prozessspeicher kurzfristig beanspruchen.
- Das Uploadlimit garantiert nicht, dass ein stark aufgeblähter geparster DataFrame in das separat konfigurierte Cachebudget passt.
- Die Tokenbindung an die gehashte Client-IP kann bei einem IP-Wechsel eine erneute Übertragung erforderlich machen.
- Die automatische Keyring-Pflege benötigt die laufende Installation auf dem angegebenen lokalen Port. Ist sie nicht erreichbar, bricht die Rotation sicher ab; der bewusste Invalidation-Override kann bestehende Links unprüfbar machen.
- Ein gesetztes `SHARE_PUBLIC_KEYRING` hat Vorrang vor der Keyring-Datei; ein unvollständiger Inline-Keyring kann deshalb trotz korrekt gepflegter Datei ältere Schlüssel aus der laufenden Konfiguration ausblenden.
- Ein Passwort muss getrennt vom verschlüsselten Link übermittelt werden, ist nicht wiederherstellbar und schützt nur so gut wie seine Entropie gegen Offline-Raten.
- Das signierte Hinweisdatum wird nur lokal als Öffnungsfrist geprüft; es löscht oder widerruft den weiterhin im Fragment vorhandenen Inhalt nicht. Für Vertraulichkeit ist das getrennt übermittelte optionale Passwort entscheidend.
- Große oder stark segmentierte Ergebnisse können das Linkgrößenlimit überschreiten.
- Effektive Gruppen unter `n = 2` werden nicht analysiert und jede ursprüngliche Gruppe vor Top N unter `n = 5` blockiert die Freigabe; diese Schwellen garantieren keine fachliche Aussagekraft oder Anonymität.
- KDE-Dichtegipfel und Modalitätsheuristik hängen von Bandbreite und gegebenenfalls Stichprobe ab und sind keine formalen Hypothesentests.
- Numerisch nicht stabil darstellbare Extrembereiche werden abgewiesen, statt irreführende Kurvenwerte zu liefern.
- Der failsafe Containerwechsel minimiert Risiken, ist auf einem einzelnen Hostport aber nicht vollständig unterbrechungsfrei.
- Fachliche Datenschutz-, Aufbewahrungs- und Freigabeanforderungen müssen zusätzlich zu den technischen Schutzmaßnahmen bewertet werden.

## Projektstruktur

```text
webserver/
├── main.py                       Flask-Anwendung und API
├── static/                       JavaScript, CSS, PWA und Service Worker
├── templates/                    HTML-Template
├── design-system/                wiederverwendbare Design-Tokens
├── Dockerfile                    gehärteter Production-Container
├── deploy.ps1 / deploy.sh        Healthcheck-Deployment mit Rollback
├── provision-share-key.*         Schlüsselprovisionierung und Rotation
├── requirements.txt              gepinnte Python-Abhängigkeiten
├── test_share.py                 Backend-, API- und Sicherheitsregressionen
├── test_tsv_export.js            TSV-/Formula-Injection-Regressionen
├── test_frontend_features.js     UI- und Share-Kryptografie-Regressionen
├── CHANGELOG.md                  Release-Änderungen
└── VERSION                       semantische Anwendungsversion
```
