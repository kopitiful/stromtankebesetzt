# Stromtanke besetzt?

Karte aller öffentlichen Ladestationen in NRW mit echter Live-Steckerbelegung
(frei / belegt / außer Betrieb) – wo verfügbar.

## Datenquelle

[OCPDB](https://www.mobidata-bw.de/dataset/e-ladesaulen) (Open ChargePoint
DataBase) der NVBW. Trotz des Namens **bundesweit**: hier bündelt die NVBW
die Pflichtmeldungen aller Ladeinfrastruktur-Betreiber nach der
**EU-AFIR-Verordnung** – seit 14.04.2025 müssen Betreiber ihre Daten
(Standort, Steckertypen **und Live-Status**) kostenlos über die
Mobilithek/den nationalen Zugangspunkt bereitstellen. Das OCPDB fasst das im
offenen OCPI-Format zusammen:

```
https://api.mobidata-bw.de/ocpdb/api/public/ocpi/3.0/locations?offset=0&limit=1000
```

Kein API-Key, keine Kosten, kein Scraping. Von den Betreibern, die
Live-Status liefern (u. a. EnBW, Tesla, Chargecloud, ecomovement,
e-clearing.net), bekommt man den tatsächlichen Steckerstatus
(`AVAILABLE`/`CHARGING`/`OUTOFORDER`/...); Stationen, die nur über das
Basisregister (BNetzA) gemeldet sind, zeigen "keine Live-Daten".

**Warum nicht Google Maps?** Getestet und verworfen: Google-Maps-
"Besucherfrequenz" (Popular Times) existiert für Ladestationen praktisch
nicht (0 von 5 getesteten NRW-Stationen hatten Daten) – die Geräte sammeln
schlicht zu wenig "Besuchs"-Signal für diese Google-Funktion. OCPDB liefert
echte, offizielle, kostenlose Live-Daten statt eines Rate-Limit-Problems.

## Funktionsweise

1. `scripts/fetch_stations.py` lädt täglich (GitHub Action) den kompletten
   bundesweiten OCPDB-Datensatz (paginiert, ~110 Requests), filtert auf NRW
   (PLZ-Präfix bzw. `state`-Feld) und dedupliziert Standorte, die sowohl im
   BNetzA-Basisregister als auch bei einer Live-Quelle auftauchen.
2. Pro Standort wird der Status aus den Ladepunkt-Stati abgeleitet:
   - **frei** – mind. 1 Ladepunkt `AVAILABLE`
   - **besetzt** – alle `CHARGING`/`RESERVED`, keiner frei
   - **außer Betrieb** – `OUTOFORDER`/`INOPERATIVE`/`REMOVED`/`BLOCKED`
   - **keine Live-Daten** – nur BNetzA-Basisregister, kein Live-Feed
3. Zusaetzlich wird pro Standort per Regex-Heuristik erkannt, ob es sich um
   eine Autobahn-Raststätte/einen Autohof handelt (Name/Adresse enthält
   "Raststätte"/"Rasthof"/"Autohof"/"BAB" oder einen Autobahn-Bezug wie "A3",
   "A555" als eigenständiges Wort) – Feld `autobahn: true/false`. Nur ein
   Erkennungs-Heuristik, kein amtliches Merkmal: erfasst nicht jede Station
   an einer Autobahn, aber ohne Fehlalarme bei normalen Straßennamen.
4. Ergebnis landet in `docs/data/latest.json` (~20.600 NRW-Standorte, nach
   Filterung auf `country=DEU` – frühere Version hatte fälschlich ~1.200
   Schweizer Stationen wegen PLZ-Präfix-Kollision mit eingerechnet),
   `docs/data/history/<datum>.json` (Tages-Archiv) und
   `docs/data/stats_history.json` (Tagesaggregat frei/besetzt/... für den
   Verlauf im Dashboard, ohne pro-Standort-Historie zu speichern).
5. `docs/index.html`: statische Leaflet-Karte mit Clustering
   (Leaflet.markercluster, da >20.000 Marker) – lädt die JSONs per
   `fetch()`, kein Server nötig, GitHub Pages reicht. Autobahn-Standorte
   bekommen einen cyanfarbenen Ring; Checkbox im Panel blendet alle
   anderen aus ("nur Autobahn-Raststätten").

## Setup

1. GitHub Pages auf Branch `main`, Ordner `/docs` aktivieren.
2. Fertig – kein API-Key, kein Secret nötig.

## Wartezeit-Schätzung ("lädt seit X · könnte in ~Y Min fertig sein")

Keine Quelle (Google, OCPI) meldet eine echte Restzeit. Was es gibt:
`evse.last_updated` aus dem Live-Feed – der Zeitpunkt, zu dem sich der
Status dieses Ladepunkts zuletzt geändert hat. Daraus wird:

1. **"lädt seit mind. X Min/Std"** – `jetzt - last_updated` des am
   längsten ladenden Ladepunkts am Standort. Faktenbasiert (Mindestdauer),
   kein Schätzwert.
2. **"könnte in ~Y Min fertig sein"** – nur bei DC-Schnellladern (≥43 kW):
   `typische Sessiondauer bei dieser kW-Zahl - bereits verstrichene Zeit`.
   Die typische Dauer wird **stufenlos nach der tatsächlichen Ladeleistung
   der Station** interpoliert (nicht nur 3 grobe Stufen), anhand von
   ADAC/Herstellerangaben 2026 als Stützpunkte: 43 kW ≈ 55 Min, 75 kW ≈ 40 Min,
   100 kW ≈ 32 Min, 150 kW ≈ 24 Min, 300 kW ≈ 20 Min (jeweils ca. 10–80 %).
   Bewusst **keine** Rechnung "Durchschnittsbatterie ÷ kW": an starken
   HPC-Ladern stehen im Schnitt andere (größere, schneller ladende) Autos
   als an schwachen 50-kW-Säulen – eine einzelne Batteriegröße über alle
   Leistungsklassen hinweg würde die Schätzung eher verschlechtern, die
   ADAC-Realwerte bilden diesen Effekt bereits ab.
   **Reine Schätzung**, keine Messung dieser konkreten Session – das
   tatsächliche Fahrzeug kann kürzer oder länger laden, und selbst wenn es
   fertig ist, könnte es stehen bleiben (siehe Hinweistext im Popup). Bei
   AC-Ladern (<43 kW) wird bewusst keine Schätzung gezeigt, da Sessions dort
   Stunden statt Minuten dauern und zu stark streuen.

## Grenzen

- Nur Standorte mit einer der Live-Quellen (aktuell gut die Hälfte aller
  NRW-Standorte) zeigen echten frei/belegt-Status. Der Rest zeigt nur die
  Position (BNetzA-Basisregister ohne Live-Meldung).
- "Besetzt" heißt: alle gemeldeten Ladepunkte laden/reserviert – das
  OCPI-Feld bildet den tatsächlichen Stecker-Status ab, keine Warteschlange
  davor.
- Die Minuten-Schätzung ist ein Erfahrungswert, keine Garantie – siehe oben.
- Mehr Live-Abdeckung kommt mit der Zeit automatisch, sobald mehr Betreiber
  ihre AFIR-Pflicht per Live-Feed statt nur Basisregister erfüllen.
