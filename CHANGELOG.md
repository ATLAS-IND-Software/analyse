# Changelog

## 3.0.0 – 2026-07-31

### Added

- Datencheck mit Vorschau der ersten zehn Zeilen, Spaltenprofilen, Qualitätswarnungen, manuellen Importregeln, Anzeigenamen und Einheiten
- locale-sichere Zahleninterpretation einschließlich Dezimalkomma und konfigurierbarer Tausendertrennzeichen
- kurzlebiger, größen- und anzahlbegrenzter RAM-Uploadcache mit clientgebundenem Token zur Vermeidung wiederholter Uploads
- `/api/estimate` für Segmentumfang, Gruppengrößen, Kardinalitäten, Ausschlüsse, Kleingruppen und Kurvenlimit
- Kardinalitätswarnungen und kollisionsfreie, strukturell markierte Top-N-/Other-Gruppierung je Segmentspalte
- interaktives Dichtediagramm mit Tooltip, schaltbarer Legende, Histogramm, Rug-Plot, Mittelwert-/Medianlinien und Bereichszoom
- sichtbare Datenabdeckung mit dargestellten Zeilen sowie Anzahl und Zeilenumfang ausgelassener Kleingruppen im Ergebniskopf und in TSV-/SVG-Reproduzierbarkeitsangaben
- SVG- und PNG-Diagrammexport mit eingebetteter Legende der sichtbaren Reihen sowie Reproduzierbarkeitsmetadaten im SVG- und TSV-Export
- Quartile, IQR, MAD, KDE-Dichtegipfel, diskreter Modus und 95-%-Student-t-Konfidenzintervall des Mittelwerts
- frei einstellbare KDE-Bandbreite und deterministische KDE-/Rug-Stichproben mit konfigurierbaren Obergrenzen
- transparente Ausschlusszählung für fehlende, ungültige, nicht-endliche und nicht segmentierbare Werte
- signierte Reproduzierbarkeitsangaben mit Import-, relevanter Spalten-, Segment-, Top-N- und KDE-Konfiguration
- signierte Hinweisdatumsangaben für lokal geprüfte Öffnungsfristen von Freigabelinks
- automatisch gepflegter Public-Key-Keyring zur Verifikation bestehender Links nach einer Schlüsselrotation
- optional passwortgeschützte Freigabelinks mit clientseitigem AES-256-GCM und PBKDF2-HMAC-SHA-256
- zugängliche Tabellensortierung, Statusmeldungen, Diagrammzusammenfassung, Tastaturfokus, Reduced-Motion-Regeln und responsive Mobilansicht
- Health-Metadaten zu prozesslokalem Uploadcache, Analyseanzahl, Laufzeit und Berechnungsdauer

### Changed

- Der Frontend-Ablauf verwendet nach dem ersten Upload im Normalfall den temporären Token für erneute Inspektion, Schätzung und Analyse; bei abgelaufenem Token bleibt ein Datei-Fallback erhalten.
- ID-artige, nahezu eindeutige monotone und konstante Spalten werden bei der X-Vorauswahl abgewertet.
- Der diskrete Modus wird nur noch bei einem eindeutig häufigsten wiederholten Wert ausgewiesen; gebundene Modi werden als Werteliste ohne willkürliche Einzelauswahl geliefert und der KDE-Dichtegipfel wird getrennt angegeben.
- Numerische Bandbreitenwerte multiplizieren den Scott-Faktor (`1,0 = Scott`), während Scott und Silverman als benannte Regeln erhalten bleiben.
- Kurven- und Histogrammwerte werden ohne verlustreiche feste Acht-Dezimal-Rundung serialisiert; instabile Extrembereiche werden verständlich abgewiesen.
- KDE-Kurven großer Gruppen verwenden eine deterministische Stichprobe; Kennzahlen und Histogramme bleiben exakt auf allen endlichen Werten.
- Modalität wird ausdrücklich als bandbreitenabhängige KDE-Peak-Heuristik ausgewiesen.
- Fehlende, ungültige und nicht-endliche Messwerte sowie fehlende Segmentwerte werden transparent gezählt und ausgeschlossen.
- Nicht-endliche X-Werte werden ausgeschlossen; `+Infinity` und `-Infinity` in Segmentspalten bleiben als getrennte kanonische Kategorien erhalten.
- Breite Tabellen werden nach dem Filtern früh auf benötigte Analysefelder projiziert, und Estimate materialisiert keine vollständigen Analyseserien.
- Breite UND-/ODER-Filterbäume kombinieren ihre Kindmasken fortlaufend, sodass unabhängig von der Zahl direkter Kinder nur Ergebnis- und aktuelle Kindmaske gleichzeitig gehalten werden.
- Zeilen in effektiven Gruppen nach Top N mit `n < 2` werden über `plotted_rows`, `omitted_small_group_count` und `omitted_small_group_rows` nachvollziehbar von tatsächlich dargestellten Zeilen getrennt.
- Datenschutzhinweise unterscheiden nun ausdrücklich zwischen fehlender dauerhafter Nutzdatenspeicherung und dem kurzlebigen Prozesszustand während der Cache-TTL.
- Der direkte Flask-Entwicklungsstart bindet standardmäßig nur an `127.0.0.1`; eine abweichende Bind-Adresse muss ausdrücklich über `HOST` gesetzt werden. Docker/Gunicorn bindet intern weiterhin an `0.0.0.0`, während das Deployment den Host-Port auf Loopback beschränkt.

### API

- Neuer Endpunkt `POST /api/estimate`
- `/api/inspect` liefert effektive Importregeln, Vorschau, Spaltenprofile, Qualitätswarnungen, Upload-Token und TTL.
- `/api/estimate` und `/api/analyze` liefern `plotted_rows`, `omitted_small_group_count` und `omitted_small_group_rows`; Analyze führt ausgelassene Zeilen zusätzlich in `exclusions` und die beiden Omitted-Felder in der Reproduzierbarkeit. `small_group_count` bleibt in Estimate kompatibel erhalten.
- `/api/analyze` liefert außerdem Histogramm, Rug, Referenzwerte, Methodik, Ausschlüsse, Reproduzierbarkeit, Timing und Uploadquelle.
- `/api/share-key` liefert aktuellen Schlüssel und historischen Keyring unter Beibehaltung der bisherigen Felder.
- `/health` liefert `uptime_seconds`, Cachegrenzen/-belegung, Analysemetriken, getrennte Inspect- und Analyseparallelitätsgrenzen, KDE-Stichprobengrenze und `state_scope: process_local`.

### Security

- Verschlüsselte Freigabelinks verwenden im Browser AES-256-GCM mit zufälligem 16-Byte-Salt, zufälliger 12-Byte-IV und einem über 250.000 PBKDF2-HMAC-SHA-256-Iterationen abgeleiteten Schlüssel.
- Das Hinweisdatum der lokalen Öffnungsfrist und die Reproduzierbarkeitsinformationen sind Bestandteil des Ed25519-signierten Payloads; die Frist ist kein serverseitiger Widerruf und entfernt den Inhalt nicht aus dem URL-Fragment.
- Linkpasswörter werden weder an den Server gesendet noch in den Link aufgenommen und müssen getrennt übermittelt werden.
- Rug-Stichproben mit beobachteten Einzelwerten bleiben lokal und werden vor der Ed25519-Signierung aus der Freigabekopie entfernt.
- Spaltenkonfiguration und spaltenbezogene Segmentmetadaten werden im signierten Freigabepayload auf die X-Spalte und tatsächlich ausgewählte Segmentspalten minimiert; Alias- und Einheitenangaben unbeteiligter Spalten bleiben lokal.
- Kleingruppen werden für die Freigabesperre vor Top N auf den ursprünglichen Segmentgruppen geprüft; Bündelung kann die Sperre nicht umgehen.
- Ein serverseitiges, auf das 2-MB-Browserlimit gedeckeltes Größenlimit unterdrückt übergroßes Freigabematerial, ohne die lokale Analyse zu verwerfen.
- Container laufen mit festem PID-Limit, schreibgeschütztem Root-Dateisystem, begrenztem `/tmp`, entfernten Capabilities und `no-new-privileges`.
- Gunicorn übernimmt per `exec` die PID-1-Rolle; Deployment und Container verwenden ein 45-Sekunden-Stop-Timeout für geordnete Terminierung.
- Private Schlüssel und Rotationsbackups erhalten unter Windows eine nicht vererbte ACL für aktuellen Benutzer, `SYSTEM` und Administratoren; unter POSIX erhalten sie ebenso wie neu geschriebene Keyring-Dateien Modus `0600`.
- Deployments reichen den privaten Seed über eine nur kurz bestehende Docker-`--env-file` statt über die Prozessargumentliste ein. Windows schützt temporäres Verzeichnis und Datei mit privater DACL, POSIX mit Modus `0600`; die Skripte entfernen die Datei unmittelbar nach `docker run` auch im Fehlerpfad.

### Operations

- Deploy-Skripte unterstützen Uploadlimit, Estimate-Rate-Limit, getrennte Inspect-/Analyseparallelität, Cache-TTL/-Größe/-Einträge, KDE-/Rug-Grenzen, Inline- oder dateibasierten historischen Public-Key-Keyring sowie CPU- und RAM-Limits.
- Standardlimits: 1 GB RAM, 2 CPUs, 256 PIDs, 256 MB Cache pro Worker, 600 Sekunden TTL, 100 Cacheeinträge, 20.000 KDE- und 300 Rug-Werte je Kurve.
- Estimate und Analyse teilen sich die konfigurierten prozesslokalen Parallelitätsslots.
- Inspect verwendet unabhängig davon standardmäßig einen prozesslokalen Slot; bei Belegung antwortet der Endpunkt mit `503` und `Retry-After: 5`.
- Rotationsskripte sichern den laufenden Public Key vor der privaten Rotation atomar in `.share-public-keyring.json`, brechen bei fehlender Verifikation ab und bieten einen expliziten Invalidation-Override.
- Mehrere Gunicorn-Worker benötigen einen gemeinsamen externen Kurzzeitcache oder Session-Stickiness.

### Tests

- Erweiterte Backendregressionen für Importregeln, Token-Cache, Inspect-/Analyseparallelität, speicherschonende Filtermasken, Estimate/Top N, ausgelassene Kleingruppenzeilen, Statistik, Ausschlüsse, Sampling, lokale Öffnungsfrist, Keyring und Health-Metadaten
- neuer Frontendvertragstest für 27 UI-Hooks, Accessibility-/Responsive-Regeln sowie AES-GCM/PBKDF2-Roundtrip und falsches Passwort
- bestehender TSV-/Formula-Injection-Test bleibt Bestandteil des Release-Gates
