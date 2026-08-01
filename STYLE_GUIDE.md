# Gemeinsames Web-CD

Version 1.0 · abgeleitet aus **Histo Maker 2.4.1** · Stand 31. Juli 2026

Dieser Leitfaden ist die verbindliche visuelle und interaktive Grundlage für nachfolgende Webprojekte. Er überträgt das bestehende Erscheinungsbild in ein wiederverwendbares System. Produktname, Fachtexte, Daten und Funktionsumfang dürfen variieren; Farben, Typografie, Formensprache, Abstände, Zustände und grundlegendes Seitenraster bleiben konsistent.

Die technische Quelle ist [`design-system/tokens.css`](design-system/tokens.css). Neue Projekte kopieren oder importieren diese Datei, bevor sie eigene Komponentenstile ergänzen.

## 1. Gestaltungsprinzipien

1. **Ruhig und präzise.** Viel helle Fläche, klare Hierarchie und sparsame Akzente. Die Oberfläche wirkt wie ein hochwertiges Arbeitswerkzeug, nicht wie eine Marketingseite.
2. **Vertrauen sichtbar machen.** Datenschutz, Systemstatus und wichtige Einschränkungen werden nahe an der betreffenden Aktion erklärt.
3. **Daten vor Dekoration.** Farbe trägt Bedeutung. Effekte bleiben subtil und dürfen Tabellen, Diagramme oder Formulare nie überlagern.
4. **Editorial trifft funktional.** Große, ruhige Serifentitel geben Charakter; Bedienung, Metadaten und Daten bleiben in einer nüchternen Sans-Serif.
5. **Eine klare Primäraktion.** Pro Bereich gibt es höchstens eine dominante Aktion in Navy. Mint markiert Fokus, Fortschritt und positive Bedeutung.

## 2. Verbindlicher CD-Kern

### Farben

| Rolle | Token | Wert | Verwendung |
|---|---|---:|---|
| Haupttext | `--color-ink` | `#101828` | Fließtext, Überschriften |
| Primärfläche | `--color-navy` | `#0b1220` | Primärbutton, Markenfläche, Toast |
| Sekundärtext | `--color-muted` | `#667085` | Erklärungen, Metadaten |
| Seitenhintergrund | `--color-ground` | `#f3f5f7` | App-Hintergrund |
| Kartenfläche | `--color-paper` | `#ffffff` | Karten, Dialoge, Eingaben |
| Trennlinie | `--color-line` | `#e4e9ef` | Rahmen und Divider |
| Primärakzent | `--color-mint` | `#58d6aa` | Akzent auf dunkler Fläche, Markengrafik |
| Aktiver Akzent | `--color-mint-dark` | `#18795b` | Links/Fokus/aktive Zustände auf hellen Flächen |
| Sekundärakzent | `--color-amber` | `#f0b45b` | kleine dekorative oder warnende Akzente |
| Fehler | `--color-danger` | `#9b2c2c` | Fehlertext und Fehler-Toast |

Navy, Mint und Amber bilden die Erkennungskombination. Mint und Amber in ihrer hellen Ausprägung niemals als kleinen Text auf Weiß einsetzen; dafür die dunklen semantischen Farben verwenden. Reines Schwarz kommt nicht vor.

Für Diagramme gilt die feste Reihenfolge `--chart-1` bis `--chart-8`. Das zweite Serien-Amber wurde in der Token-Datei gegenüber dem Bestand auf `#b27316` abgedunkelt, damit es auf Weiß ausreichend erkennbar ist. Serien werden zusätzlich immer durch Legende, Label oder Symbol unterschieden; Farbe allein reicht nicht.

### Typografie

- **UI und Fließtext:** `Inter`, danach System-Sans-Serif. Inter wird nur verwendet, wenn es lokal oder datenschutzkonform bereitgestellt wird; es gibt keinen externen Font-Zwang.
- **Display und Abschnittstitel:** `Georgia`, normal bis medium (`400–500`).
- **Code und technische Werte:** System-Monospace.
- **Hero-Titel:** `clamp(44px, 5vw, 76px)`, Zeilenhöhe `1.01`, Laufweite `-0.05em`.
- **Kartentitel:** `21px`, Georgia, Gewicht `500`.
- **Fließtext:** `16–18px`, Zeilenhöhe `1.6–1.7`.
- **Bedienung:** `12–14px`, Gewicht `600–700`.
- **Eyebrow/Label:** `10–11px`, Versalien, Gewicht `750–800`, Laufweite `0.06–0.16em`.

Die Serifenschrift ist Titeln vorbehalten. Formfelder, Buttons, Tabellen und Zahlen verwenden immer Sans-Serif. Versalien nur für kurze Kategorien und Feldbezeichnungen, nie für längere Sätze.

### Abstände, Radien und Schatten

Neue Layouts verwenden bevorzugt die Tokens `--space-1` bis `--space-9`. Innerhalb von Komponenten dominieren 8, 12 und 16 Pixel; Karten erhalten 24 oder 32 Pixel Innenabstand; zwischen großen Seitenbereichen liegen 48 bis 72 Pixel.

| Element | Radius | Schatten |
|---|---:|---|
| Eingabe, Button | `9px` | keiner |
| Statusbox, kleine Gruppe | `10px` | keiner |
| Karte, Dialog | `18px` | `--shadow-card` bzw. `--shadow-dialog` |
| große Upload-/Featurefläche | `24px` | nur bei Hover `--shadow-float` |
| Pill/Statuspunkt | `999px` | keiner |

Schatten bleiben kühl, weich und sehr transparent. Rahmen sind die primäre Flächentrennung.

## 3. Seitenraster

- Maximale Inhaltsbreite: `1540px`, horizontal zentriert.
- Desktop-Seitenrand: `38px`; Tablet: `18px`; Mobil: `12px`.
- Topbar: `92px`, mobil `74px`, mit einer feinen unteren Trennlinie.
- Arbeitsansicht: linke Steuerung `430px`, Ergebnisbereich flexibel, Abstand `22px`.
- Die Steuerung ist am Desktop sticky und wird unter `1000px` einspaltig.
- Hero: zwei ausgewogene Spalten; unter `900px` einspaltig.
- Kompakte mobile Anpassungen beginnen bei `560px`; Dialog- und Aktionsanpassungen bei `650px`.

Responsive Verhalten entsteht durch Umbruch und Neuordnung, nicht durch bloßes Verkleinern. Primäraktionen bleiben auf kleinen Displays mindestens so breit und hoch, dass sie sicher bedienbar sind.

## 4. Komponenten

### Kopfzeile und Produktmarke

Links stehen Markenzeichen, Produktname und eine kurze Kategorie. Rechts stehen höchstens ein kompakter Vertrauenshinweis und eine sekundäre Aktion. Die Marke besteht aus einer Navy-Fläche mit Mint-Grundelementen und optional einem Amber-Akzent; der konkrete Produktname darf wechseln.

### Karten

Karten liegen weiß auf dem hellgrauen Grund, haben `18px` Radius, eine fast unsichtbare Navy-Rahmenlinie und `--shadow-card`. Eine Karte enthält genau einen thematischen Bereich. Kartentitel kombinieren optional eine Mint-Eyebrow mit einem Georgia-Titel.

### Buttons

- **Primary:** Navy-Fläche, weißer Text, Mint-Pfeil/Indikator; Höhe etwa `49px` bei Hauptaktionen.
- **Ghost:** weiße Fläche, graue Kontur, dunkler Text.
- **Secondary:** hellgraue Fläche ohne starke Kontur.
- **Danger:** nur für destruktive Aktionen; dunkles Rot, nie als allgemeiner Akzent.

Buttons sind semibold/bold, `9px` gerundet und bewegen sich beim Hover höchstens `1px` nach oben. Disabled-Zustände haben reduzierte Deckkraft und keine Bewegung. Jede sichtbare Hover-Reaktion braucht auch einen klaren `:focus-visible`-Zustand.

### Formulare

Eingaben sind `42px` hoch, auf `#fbfcfd`, mit `1px` Kontur und `9px` Radius. Labels stehen darüber als kurze Versalien. Fokus: Mint-Dark-Kontur plus ein `3px` Mint-Fokusring. Zusammengehörige Felder stehen zweispaltig und brechen mobil auf eine Spalte um.

### Status und Feedback

- **Erfolg/Verifikation:** Mint-Soft-Fläche, Mint-Dark-Text, grüne Kontur.
- **Warnung/Bestätigung:** Amber-Soft-Fläche, brauner Text.
- **Fehler:** sehr helle Rotfläche mit dunkelrotem Text; Toasts dürfen vollflächig dunkelrot sein.
- **Laden:** heller, leicht unscharfer Overlay; Spinner mit Mint-Dark als aktivem Segment.
- **Toast:** unten rechts, Navy, weißer Text, maximal ca. `380px`; mobil mit sicherem Seitenabstand.

Status wird nie ausschließlich über Farbe vermittelt. Immer Icon, Überschrift oder eindeutigen Text ergänzen.

### Tabellen und Datenvisualisierung

Tabellen sitzen in einem eigenen gerundeten Rahmen und dürfen horizontal scrollen. Header sind sticky, hellgrau und als kurze Versalien gesetzt. Zahlen sind rechtsbündig, Bezeichnungen linksbündig; alternierende Zeilen und Mint-Hover erleichtern das Lesen.

Diagramme verwenden feine graue Rasterlinien, dezente Achsen und `2.5px` starke, abgerundete Serienlinien. Die Legende steht direkt beim Diagramm. Technische Mindestanforderungen:

- SVG erhält `role="img"` und einen aussagekräftigen zugänglichen Namen.
- Farbserien folgen der festgelegten Palette.
- Achsenwerte, Einheit und Datenumfang sind textlich erkennbar.
- Leere Zustände erklären den nächsten sinnvollen Schritt.

### Dialoge

Dialoge sind maximal etwa `620px` breit, besitzen `18px` Radius und einen dunklen, leicht weichgezeichneten Backdrop. Titel und Schließen-Aktion stehen oben. Risiko- oder Datenschutzinformation erscheint vor der auslösenden Primäraktion, nicht danach.

## 5. Sprache und Inhalt

- Deutsch, direkt, sachlich und freundlich.
- Handlungsbuttons beginnen mit einem Verb: „Analyse erstellen“, „Link teilen“, „Datei auswählen“.
- Leere Zustände bestehen aus kurzer Überschrift plus einem Satz zum nächsten Schritt.
- Fachbegriffe werden verwendet, wenn sie Präzision schaffen; eine kurze Erklärung steht direkt daneben.
- Datenschutz- und Vertrauenshinweise beschreiben konkrete Tatsachen statt allgemeiner Werbeversprechen.
- Auslassungspunkte als Zeichen `…`, Gedankenstrich `–`, Multiplikation/Trennung mit `·` konsistent verwenden.

## 6. Barrierefreiheit und Interaktion

Folgende Regeln sind für neue Projekte verpflichtend:

- Normale Texte erfüllen mindestens WCAG 2.2 AA (`4.5:1`), große Texte mindestens `3:1`.
- Alle Funktionen sind per Tastatur erreichbar; die Fokusreihenfolge folgt der visuellen Reihenfolge.
- Interaktive Ziele sind nach Möglichkeit mindestens `44 × 44px` groß.
- `:focus-visible` ist deutlich und wird nie ohne Ersatz entfernt.
- Icons mit Textwiederholung sind `aria-hidden`; reine Iconbuttons besitzen einen zugänglichen Namen.
- Fehler stehen zusätzlich direkt am betroffenen Feld und werden für Assistenztechnik angekündigt.
- Animationen respektieren `prefers-reduced-motion`; die Token-Datei setzt Bewegungsdauern dann auf null.
- Native semantische Elemente (`button`, `label`, `table`, `dialog`) haben Vorrang vor nachgebauten Rollen.

## 7. Technischer Start für Folgeprojekte

```css
@import url("./design-system/tokens.css");

* { box-sizing: border-box; }

html {
  color: var(--color-ink);
  background: var(--color-ground);
  font-family: var(--font-ui);
}

body {
  min-width: 320px;
  margin: 0;
  background:
    radial-gradient(circle at 12% 4%, #fff 0, transparent 28%),
    var(--color-ground);
}

:focus-visible {
  outline: 2px solid var(--color-mint-dark);
  outline-offset: 3px;
}
```

Empfohlene Reihenfolge im Projekt:

1. `tokens.css` unverändert übernehmen.
2. Globalen Reset und Grundtypografie definieren.
3. Gemeinsame Komponenten auf Tokens aufbauen.
4. Projektspezifische Layouts und Fachkomponenten ergänzen.
5. Visuell bei `1440px`, `900px`, `560px` und `320px` prüfen.
6. Tastaturbedienung, Kontrast, Zoom bei `200 %`, Reduced Motion sowie leere, Lade-, Fehler- und Erfolgszustände testen.

## 8. Governance

### Darf pro Projekt variieren

- Produktname, Unterzeile und fachliche Eyebrow
- Inhalt und Reihenfolge von Karten
- Anzahl der Navigationspunkte
- Visualisierungstyp und projektspezifische Fachkomponenten
- Illustrationen, solange sie die Navy/Mint/Amber-Sprache aufnehmen

### Bleibt projektübergreifend stabil

- Kernfarben und Diagrammreihenfolge
- Serif/Sans-Hierarchie
- Radien, Schattencharakter und Fokusdarstellung
- Buttonhierarchie und semantische Statusfarben
- Grundraster, responsive Prinzipien und Inhaltsbreite
- sachliche deutsche Tonalität und Vertrauenskommunikation

Neue Tokens werden nur ergänzt, wenn mindestens zwei Projekte denselben Bedarf haben. Bestehende Tokenwerte werden zentral versioniert statt lokal überschrieben. Projektspezifische Ausnahmefarben erhalten keine Marken-Token-Namen und dürfen die Kernpalette nicht verdrängen.

## 9. Abnahmecheckliste

- [ ] Token-Datei eingebunden; keine unnötigen Duplikate von Kernfarben
- [ ] Höchstens eine dominante Primäraktion pro Bereich
- [ ] Georgia nur für Display-/Abschnittstitel
- [ ] Karten, Formfelder, Buttons und Dialoge folgen den festgelegten Radien
- [ ] Alle Interaktionszustände vorhanden: default, hover, focus, disabled, loading, error, success
- [ ] Status nicht nur über Farbe kommuniziert
- [ ] Diagrammfarben in festgelegter Reihenfolge und mit Legende/Labels
- [ ] Layout bei Desktop, Tablet, Mobil und `200 %` Zoom geprüft
- [ ] Vollständig mit Tastatur bedienbar und Fokus sichtbar
- [ ] Datenschutz-/Risikohinweise stehen vor der relevanten Aktion
- [ ] Keine externen Schriften oder Tracking-Ressourcen ohne bewusste Freigabe
