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
    plz = (item.get("postal_code") or "")[:2]
    return plz in NRW_PLZ_PREFIX


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
    statuses = []
    total = 0
    for pool in item.get("charging_pool", []):
        for evse in pool.get("evses", []):
            total += 1
            statuses.append(evse.get("status"))
    return statuses, total


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


def build_station_meta(item):
    _, total = evse_statuses(item)
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
    }


def build_station_status(item):
    statuses, _ = evse_statuses(item)
    status = aggregate_status(statuses)
    return {
        "id": item.get("id"),
        "status": status,
        "color": STATUS_COLOR[status],
        "available": statuses.count("AVAILABLE"),
        "source": item.get("source") if status != "unbekannt" else None,
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

    meta = [build_station_meta(it) for it in deduped]
    status_rows = [build_station_status(it) for it in deduped]

    now = datetime.now(TZ)
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
