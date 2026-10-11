#!/usr/bin/env python3
"""Calcula els pesos del MSCI World per regió (Nord Amèrica, Europa, Pacífic) i els desa a msci-weights.json.

Font principal: fitxer públic de posicions de l'ETF iShares MSCI World (suma el pes per país).
Font de reserva: factsheets oficials de MSCI (necessita `pdftotext`, inclòs a GitHub Actions).
Només usa la biblioteca estàndard de Python.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

ISHARES_CSV = "https://www.ishares.com/us/products/239696/ishares-msci-world-etf/latest-holdings.csv"
MSCI_WORLD_PDF = "https://www.msci.com/documents/10199/178e6643-6ae6-47b9-82be-e1fc565ededb"
MSCI_PACIFIC_PDF = "https://www.msci.com/documents/10199/255599/msci-pacific-index.pdf"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
OUT = pathlib.Path(__file__).with_name("msci-weights.json")

REGION_OF = {}
for region, countries in {
    "na": ["united states", "canada"],
    "eu": ["austria", "belgium", "denmark", "finland", "france", "germany", "ireland", "israel", "italy",
           "luxembourg", "netherlands", "norway", "portugal", "spain", "sweden", "switzerland", "united kingdom"],
    "pac": ["japan", "australia", "hong kong", "singapore", "new zealand"],
}.items():
    for c in countries:
        REGION_OF[c] = region


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip().lower()


def to_float(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def finish(na: float, eu: float, pac: float, as_of: str, source: str) -> dict:
    """Normalitza a 100 %, arrodoneix i valida que els valors siguin versemblants."""
    total = na + eu + pac
    na, pac = round(na / total, 4), round(pac / total, 4)
    eu = round(1 - na - pac, 4)
    if not (0.60 <= na <= 0.90 and 0.04 <= pac <= 0.15 and 0.08 <= eu <= 0.25):
        raise ValueError(f"Valors fora del rang esperat: na={na} eu={eu} pac={pac}")
    return {"weights": {"na": na, "eu": eu, "pac": pac}, "asOf": as_of, "source": source}


# ---------- font principal: posicions de l'ETF ----------
def parse_holdings(text: str) -> dict:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    h = next((i for i, r in enumerate(rows[:80])
              if "location" in [norm(c) for c in r] and any(norm(c).startswith("weight") for c in r)), None)
    if h is None:
        raise ValueError("Format de fitxer no reconegut.")
    hdr = [norm(c) for c in rows[h]]
    i_loc = hdr.index("location")
    i_w = next(i for i, c in enumerate(hdr) if c.startswith("weight"))
    i_asset = hdr.index("asset class") if "asset class" in hdr else None

    as_of = None
    for r in rows[:h]:
        if r and re.search(r"as of", r[0], re.I) and len(r) > 1:
            try:
                as_of = dt.datetime.strptime(r[1].strip(), "%b %d, %Y").date().isoformat()
            except ValueError:
                pass
            break

    sums = {"na": 0.0, "eu": 0.0, "pac": 0.0}
    unmapped = 0.0
    count = 0
    for r in rows[h + 1:]:
        if len(r) <= max(i_loc, i_w):
            continue
        if i_asset is not None and norm(r[i_asset]) != "equity":
            continue
        w = to_float(r[i_w])
        if w is None or w <= 0:
            continue
        count += 1
        region = REGION_OF.get(norm(r[i_loc]))
        if region:
            sums[region] += w
        else:
            unmapped += w
    total = sum(sums.values())
    if count < 300 or total <= 0:
        raise ValueError("El fitxer no conté prou posicions.")
    if unmapped / (total + unmapped) > 0.02:
        raise ValueError("Hi ha massa països sense regió assignada.")
    return finish(sums["na"], sums["eu"], sums["pac"], as_of or dt.date.today().isoformat(),
                  "iShares MSCI World ETF (suma per país)")


# ---------- font de reserva: factsheets de MSCI ----------
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def pct_after(text: str, label: str) -> float:
    m = re.search(re.escape(label) + r"\s*:?\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    if not m:
        raise ValueError(f"No s'ha trobat «{label}» al factsheet.")
    return float(m.group(1))


def parse_msci(world_text: str, pacific_text: str) -> dict:
    us, ca = pct_after(world_text, "United States"), pct_after(world_text, "Canada")
    jp_world, jp_pac = pct_after(world_text, "Japan"), pct_after(pacific_text, "Japan")
    na = us + ca
    pac = jp_world / (jp_pac / 100.0)
    eu = 100.0 - na - pac
    m = re.search(r"\b(" + "|".join(MONTHS) + r")\w*\s+(\d{1,2}),\s*(\d{4})", world_text.upper())
    as_of = dt.date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))).isoformat() if m else dt.date.today().isoformat()
    return finish(na, eu, pac, as_of, "MSCI (factsheets oficials)")


def pdf_to_text(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        return subprocess.run(["pdftotext", "-layout", f.name, "-"], check=True, capture_output=True, text=True).stdout


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def compute() -> dict:
    errors = []
    try:
        return parse_holdings(fetch(ISHARES_CSV).decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        errors.append(f"iShares: {e}")
        print(errors[-1], file=sys.stderr)
    try:
        return parse_msci(pdf_to_text(fetch(MSCI_WORLD_PDF)), pdf_to_text(fetch(MSCI_PACIFIC_PDF)))
    except Exception as e:  # noqa: BLE001
        errors.append(f"MSCI: {e}")
        print(errors[-1], file=sys.stderr)
    raise SystemExit("No s'ha pogut obtenir cap font: " + " | ".join(errors))


def main() -> None:
    new = compute()
    old = {}
    if OUT.exists():
        old = json.loads(OUT.read_text(encoding="utf-8"))
    if old.get("weights") == new["weights"] and old.get("asOf") == new["asOf"]:
        print("Sense canvis.")
        return
    new["updatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Actualitzat:", new)


if __name__ == "__main__":
    main()
