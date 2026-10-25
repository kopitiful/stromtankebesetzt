"""Tägliche Erhebung der NRW-Ladestationen inkl. echter Live-Belegung.

Quelle: OCPDB (Open ChargePoint DataBase) der MobiData BW / NVBW. Trotz des
Namens bundesweit: NVBW bündelt dort die Pflichtmeldungen aller
Ladeinfrastruktur-Betreiber nach der EU-AFIR-Verordnung (seit 14.04.2025
kostenlos über die Mobilithek bereitzustellen) im OCPI-Format – inkl.
Live-Status pro Ladepunkt (AVAILABLE/CHARGING/OUTOFORDER/...) für alle
Betreiber, die das anbieten (u.a. EnBW, Tesla, Chargecloud, ecomovement,
e-clearing.net). Keine Google-Abhängigkeit, kein API-Key, keine Kosten.

API: https://api.mobidata-bw.de/ocpdb/api/public/ocpi/3.0/locations
(paginiert über offset/limit, total_count im Response)

Ablauf:
1. Komplette bundesweite Liste paginiert abrufen.
2. Auf NRW filtern (bnetza_api-Einträge über das Feld "state", alle
   anderen Quellen über die PLZ, da dort "state" fehlt).
3. Pro Koordinate deduplizieren: wenn an einer Koordinate sowohl ein
   reiner BNetzA-Eintrag (keine Live-Daten) als auch ein Live-Quellen-
   Eintrag existiert, den BNetzA-Eintrag verwerfen (redundant).
4. Pro Standort den Status aus den Ladepunkt-Stati ableiten:
   frei / besetzt / ausser_betrieb / unbekannt (keine Live-Daten).

Output (bewusst in statisch/dynamisch getrennt, sonst waechst das Repo durch
die tägliche Speicherung von ~21.500 Standorten x volle Metadaten schnell auf
mehrere GB an):
- docs/data/stations.json       statische Metadaten (Name, Adresse, Lage,
  Ladepunktzahl) – wird jeden Tag überschrieben, wächst nicht
- docs/data/latest.json         aktueller Status (nur id/status/belegung) –
  wird überschrieben, wächst nicht
- docs/data/history/<date>.json dieselben kompakten Status-Felder als
  Tagesarchiv – alte Tage über HISTORY_RETENTION_DAYS hinaus werden gelöscht
- docs/data/stats_history.json  Tagesaggregat (Anzahl frei/besetzt/...), sehr
  klein, unbegrenzte Retention
"""
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

BASE_URL = "https://api.mobidata-bw.de/ocpdb/api/public/ocpi/3.0/locations"
PAGE_LIMIT = 1000
TZ = ZoneInfo("Europe/Berlin")

DOCS_DATA_DIR = "docs/data"
HISTORY_DIR = f"{DOCS_DATA_DIR}/history"
STATIONS_FILE = f"{DOCS_DATA_DIR}/stations.json"
LATEST_FILE = f"{DOCS_DATA_DIR}/latest.json"
STATS_HISTORY_FILE = f"{DOCS_DATA_DIR}/stats_history.json"
STATS_HISTORY_DAYS = 365
HISTORY_RETENTION_DAYS = 30

NRW_PLZ_PREFIX = {"32", "33", "40", "41", "42", "44", "45", "46", "47", "48",
                  "50", "51", "52", "53", "57", "58", "59"}

# Heuristik fuer Autobahn-nahe Standorte (Raststaetten/Autohoefe): Hinweis im
# Namen/Adresse ODER ein eigenstaendiger Autobahn-Bezug wie "A3"/"A555" als
# Wort (Wortgrenzen, damit z.B. "Am Hagen" nicht matcht).
AUTOBAHN_KEYWORDS = re.compile(
    r"\b(Rasth[o|ö]f|Raststätte|Raststaette|Autohof|BAB|Tank\s*&?\s*Rast)\b",
    re.IGNORECASE,
)
AUTOBAHN_ROUTE = re.compile(r"\bA\s?\d{1,3}\b")


def is_autobahn(item):
    operator_name = (item.get("operator") or {}).get("name") or ""
    text = f"{operator_name} {item.get('address') or ''}"
    return bool(AUTOBAHN_KEYWORDS.search(text) or AUTOBAHN_ROUTE.search(text))


def fetch_page(offset):
    url = f"{BASE_URL}?offset={offset}&limit={PAGE_LIMIT}"
    last_error = None
    for attempt in range(5):
        try:
            result = subprocess.run(["curl", "-s", "--fail", url], capture_output=True, timeout=60)
            if result.returncode != 0:
                last_error = f"curl exit {result.returncode}: {result.stderr.decode(errors='replace')}"
            else:
                return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            last_error = "timeout"
        except json.JSONDecodeError as e:
            last_error = f"JSON-Fehler: {e}"
        print(f"  Versuch {attempt + 1}/5 fuer offset={offset} fehlgeschlagen: {last_error}", file=sys.stderr)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Abruf offset={offset} nach 5 Versuchen fehlgeschlagen: {last_error}")


def fetch_all_locations():
    items = []
    offset = 0
    total = None
    while True:
        data = fetch_page(offset)
        page = data.get("items", [])
        items.extend(page)
        total = data.get("total_count", len(items))
        print(f"  offset={offset} geladen, {len(items)}/{total}")
        if not page or len(items) >= total:
            break
        offset = data.get("next_offset") or (offset + PAGE_LIMIT)

    if total is not None and len(items) < total:
        raise RuntimeError(f"Unvollstaendig: {len(items)}/{total} Standorte geladen")
    return items


def is_nrw(item):
    if item.get("source") == "bnetza_api":
        return item.get("state") == "Nordrhein-Westfalen"
    # Laender ausserhalb Deutschlands zuerst ausschliessen: Schweizer PLZ sind
    # 4-stellig und kollidieren mit unseren 2-stelligen NRW-Praefixen (z.B.
    # "4106 Therwil" matcht sonst faelschlich Praefix "41").
    country = (item.get("country") or "").upper()
    if country and country != "DEU":
        return False
    postal_code = item.get("postal_code") or ""
    if len(postal_code) != 5:
        return False
    return postal_code[:2] in NRW_PLZ_PREFIX


def dedupe(items):
    groups = defaultdict(list)
    for it in items:
        c = it.get("coordinates")
        if not c or c.get("latitude") is None:
            continue
        key = (round(c["latitude"], 5), round(c["longitude"], 5))
        groups[key].append(it)

    result = []
    for group in groups.values():
        live = [it for it in group if it.get("source") != "bnetza_api"]
        result.extend(live if live else group)
    return result


def evse_statuses(item):
    """Liefert (evse_records, total, max_power_kw). evse_records ist eine
    Liste von (status, last_updated) je Ladepunkt. max_power_kw ist die
    höchste gemeldete Nennleistung eines Pools an diesem Standort (massgeblich
    fuer die Dauer-Schaetzung: HPC laedt schneller als ein 22kW-AC-Lader)."""
    evse_records = []
    max_power_kw = 0.0
    for pool in item.get("charging_pool", []):
        power = (pool.get("max_power") or {}).get("value")
        if power:
            max_power_kw = max(max_power_kw, float(power))
        for evse in pool.get("evses", []):
            evse_records.append((evse.get("status"), evse.get("last_updated")))
    return evse_records, len(evse_records), max_power_kw


def aggregate_status(statuses):
    if not statuses:
        return "unbekannt"
    if "AVAILABLE" in statuses:
        return "frei"
    if "CHARGING" in statuses or "RESERVED" in statuses:
        return "besetzt"
    if set(statuses) & {"OUTOFORDER", "INOPERATIVE", "REMOVED", "BLOCKED"}:
        return "ausser_betrieb"
    return "unbekannt"


STATUS_COLOR = {
    "frei": "green",
    "besetzt": "red",
    "ausser_betrieb": "grey",
    "unbekannt": "lightgrey",
}

# Stuetzpunkte fuer eine typische Ladesession (ca. 10-80%), nach Anschlussleistung.
# Quelle: ADAC/Herstellerangaben 2026 (siehe README). Bewusst KEINE reine
# kWh/kW-Rechnung mit einer einzelnen "Durchschnittsbatterie": an starken
# HPC-Ladern stehen im Schnitt andere (groessere, schnellere) Autos als an
# schwachen 50kW-Saeulen, das bilden diese Realwert-Stuetzpunkte bereits ab -
# eine generische Batteriegroesse ueber alle Leistungsklassen würde die
# Schaetzung eher verschlechtern. Zwischen den Punkten wird linear interpoliert,
# damit die tatsaechliche kW-Zahl der Station einfliesst statt nur 3 grober
# Stufen.
SESSION_MIN_BY_POWER = [
    (43, 55),    # ~45-60 Min
    (50, 48),
    (75, 40),    # ~30-45 Min
    (100, 32),
    (150, 24),   # ~20-35 Min
    (300, 20),
]
MIN_POWER_FOR_ESTIMATE_KW = 43  # unter DC-Schnelllade-Schwelle keine Schaetzung (AC-Sessions dauern Stunden)


def typical_session_minutes(power_kw):
    points = SESSION_MIN_BY_POWER
    if power_kw <= points[0][0]:
        return points[0][1]
    if power_kw >= points[-1][0]:
        return points[-1][1]
    for (p0, m0), (p1, m1) in zip(points, points[1:]):
        if p0 <= power_kw <= p1:
            frac = (power_kw - p0) / (p1 - p0)
            return round(m0 + frac * (m1 - m0))
    return points[-1][1]


def build_station_meta(item):
    _, total, max_power_kw = evse_statuses(item)
    coords = item["coordinates"]
    operator = (item.get("operator") or {}).get("name") or "Unbekannter Betreiber"
    return {
        "id": item.get("id"),
        "name": operator,
        "address": item.get("address"),
        "city": item.get("city"),
        "plz": item.get("postal_code"),
        "lat": coords["latitude"],
        "lon": coords["longitude"],
        "connectors_total": total,
        "max_power_kw": max_power_kw,
        "autobahn": is_autobahn(item),
    }


def build_station_status(item, now):
    evse_records, _, max_power_kw = evse_statuses(item)
    statuses = [s for s, _ in evse_records]
    status = aggregate_status(statuses)

    occupied_since = None
    est_remaining_min = None
    if status == "besetzt":
        charging_updates = [lu for s, lu in evse_records if s in ("CHARGING", "RESERVED") and lu]
        if charging_updates:
            # frühester Start = am längsten ladend = am ehesten bald fertig
            occupied_since = min(charging_updates)
            try:
                started = datetime.fromisoformat(occupied_since.replace("Z", "+00:00"))
                elapsed_min = (now.astimezone(started.tzinfo) - started).total_seconds() / 60
                if max_power_kw >= MIN_POWER_FOR_ESTIMATE_KW:
                    typical = typical_session_minutes(max_power_kw)
                    est_remaining_min = max(0, round(typical - elapsed_min))
            except ValueError:
                pass

    return {
        "id": item.get("id"),
        "status": status,
        "color": STATUS_COLOR[status],
        "available": statuses.count("AVAILABLE"),
        "source": item.get("source") if status != "unbekannt" else None,
        "occupied_since": occupied_since,
        "est_remaining_min": est_remaining_min,
    }


def prune_old_history():
    if not os.path.isdir(HISTORY_DIR):
        return
    files = sorted(f for f in os.listdir(HISTORY_DIR) if f.endswith(".json"))
    for f in files[:-HISTORY_RETENTION_DAYS]:
        os.remove(os.path.join(HISTORY_DIR, f))


def main():
    print("Lade OCPDB-Standorte (bundesweit, paginiert)...")
    all_items = fetch_all_locations()
    print(f"Gesamt geladen: {len(all_items)}")

    nrw_items = [it for it in all_items if is_nrw(it)]
    print(f"NRW (vor Dedup): {len(nrw_items)}")

    deduped = dedupe(nrw_items)
    print(f"NRW (nach Dedup): {len(deduped)}")

    now = datetime.now(TZ)
    meta = [build_station_meta(it) for it in deduped]
    status_rows = [build_station_status(it, now) for it in deduped]

    today = now.strftime("%Y-%m-%d")

    os.makedirs(HISTORY_DIR, exist_ok=True)

    with open(STATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": now.isoformat(), "stations": status_rows}, f, ensure_ascii=False)

    with open(f"{HISTORY_DIR}/{today}.json", "w", encoding="utf-8") as f:
        json.dump({"date": today, "stations": status_rows}, f, ensure_ascii=False)
    prune_old_history()

    update_stats_history(status_rows, today)
    print("Fertig.")


def update_stats_history(stations, today):
    history = []
    if os.path.exists(STATS_HISTORY_FILE):
        with open(STATS_HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
    history = [row for row in history if row["date"] != today]

    counts = Counter(s["status"] for s in stations)
    history.append({
        "date": today,
        "frei": counts.get("frei", 0),
        "besetzt": counts.get("besetzt", 0),
        "ausser_betrieb": counts.get("ausser_betrieb", 0),
        "unbekannt": counts.get("unbekannt", 0),
        "total": len(stations),
    })
    history = history[-STATS_HISTORY_DAYS:]

    with open(STATS_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
