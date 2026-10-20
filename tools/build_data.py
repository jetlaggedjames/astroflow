"""Builds the compact offline data files in data/ from their open sources.

Run once by the developer (results are committed):
    python tools/build_data.py <OpenNGC dir> <HYG dir> <cities.json dir>

Sources and licences (see THIRD_PARTY.md):
  * OpenNGC (NGC/IC/Messier + addendum), CC BY-SA 4.0, Mattia Verga
  * HYG database v4.1 stars + misc/dso.csv (Caldwell numbers), CC BY-SA 4.0, David Nash / astronexus
  * cities.json (GeoNames cities > 1000 people), CC BY 4.0, GeoNames via lutangar/cities.json
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data"


def hms(s: str) -> float:
    h, m, sec = s.split(":")
    return (float(h) + float(m) / 60 + float(sec) / 3600) * 15


def dms(s: str) -> float:
    sign = -1 if s.strip().startswith("-") else 1
    d, m, sec = s.strip().lstrip("+-").split(":")
    return sign * (float(d) + float(m) / 60 + float(sec) / 3600)


def norm_id(s: str) -> str:
    """'NGC0224' -> 'NGC 224'; 'IC0001' -> 'IC 1'."""
    for pre in ("NGC", "IC"):
        if s.startswith(pre) and s[len(pre):].lstrip("0")[:1].isdigit():
            rest = s[len(pre):].lstrip("0")
            return f"{pre} {rest}"
    return s


TYPES = {
    "*": "Star", "**": "Double star", "*Ass": "Stellar association", "OCl": "Open cluster", "GCl": "Globular cluster",
    "Cl+N": "Cluster with nebula", "G": "Galaxy", "GPair": "Galaxy pair", "GTrpl": "Galaxy triplet",
    "GGroup": "Galaxy group", "PN": "Planetary nebula", "HII": "Emission nebula", "DrkN": "Dark nebula",
    "EmN": "Emission nebula", "Neb": "Nebula", "RfN": "Reflection nebula", "SNR": "Supernova remnant",
    "Nova": "Nova", "Other": "Other",
}


def build_dso(openngc: Path, hyg: Path) -> None:
    # Caldwell numbers from HYG misc/dso.csv: rows with cat2 == 'C'.
    caldwell: dict[str, list[int]] = {}
    with open(hyg / "misc" / "dso.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("cat2") == "C" and r.get("id2"):
                key = f"{r['cat1']} {r['id1']}".strip() if r.get("cat1") else (r.get("name") or "")
                caldwell.setdefault(key, []).append(int(r["id2"]))
    # OpenNGC lists some objects under a duplicate designation: follow those links.
    dup_of: dict[str, str] = {}
    for fname in ("NGC.csv", "addendum.csv"):
        with open(openngc / "database_files" / fname, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter=";"):
                if r["Type"] == "Dup":
                    target = (f"NGC {r['NGC'].lstrip('0')}" if r["NGC"] else f"IC {r['IC'].lstrip('0')}" if r["IC"] else "")
                    if target:
                        dup_of[norm_id(r["Name"])] = target
    for k in list(caldwell):
        if k in dup_of:
            caldwell.setdefault(dup_of[k], []).extend(caldwell.pop(k))
    rows = []
    for fname in ("NGC.csv", "addendum.csv"):
        with open(openngc / "database_files" / fname, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter=";"):
                if r["Type"] in ("Dup", "NonEx") or not r["RA"]:
                    continue
                name = norm_id(r["Name"])
                m = r["M"].lstrip("0")
                cnums = list(caldwell.get(name, []))
                if len(name) == 4 and name[0] == "C" and name[1:].isdigit():   # OpenNGC addendum: C009 etc.
                    cnums.append(int(name[1:]))
                    name = f"C {int(name[1:])}"
                mag = r["V-Mag"] or r["B-Mag"]
                rows.append({
                    "id": name,
                    "messier": f"M {m}" if m else "",
                    "caldwell": " ".join(f"C {c}" for c in cnums),
                    "type": TYPES.get(r["Type"], r["Type"]),
                    "ra": round(hms(r["RA"]), 5),
                    "dec": round(dms(r["Dec"]), 5),
                    "const": r["Const"],
                    "maj": r["MajAx"], "min": r["MinAx"], "pa": r["PosAng"],
                    "mag": mag,
                    "names": r["Common names"],
                    "other": ",".join(x for x in (r["Identifiers"] or "").split(",")[:2] if x),
                })
    # Caldwell objects outside NGC/IC (standard catalogue positions).
    extra = [
        ("Sh2-155", "C 9", "Emission nebula", 344.20, 62.62, "Cep", "50", "", "", "7.7", "Cave Nebula"),
        ("Mel 25", "C 41", "Open cluster", 66.75, 16.00, "Tau", "330", "", "", "0.5", "Hyades"),
        ("Coalsack", "C 99", "Dark nebula", 192.75, -62.50, "Cru", "420", "300", "", "", "Coalsack Nebula"),
    ]
    have = {c for r in rows for c in r["caldwell"].split(" C ") if c}
    have = {("C " + c.replace("C ", "").strip()) for c in have}
    for (i, c, t, ra, dec, con, maj, mi, pa, mag, nm) in extra:
        if c not in have:
            rows.append({"id": i, "messier": "", "caldwell": c, "type": t, "ra": ra, "dec": dec, "const": con,
                         "maj": maj, "min": mi, "pa": pa, "mag": mag, "names": nm, "other": ""})
    # Messier objects that are not NGC objects (M40, M45 in addendum...) are covered by the addendum.
    with gzip.open(OUT / "dso.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    nm = sum(1 for r in rows if r["messier"])
    nc = sum(1 for r in rows if r["caldwell"])
    print(f"dso.csv.gz: {len(rows)} objects ({nm} Messier, {nc} Caldwell)")


def build_stars(hyg: Path, maglim: float = 9.0) -> None:
    src = hyg / "hyg" / "CURRENT" / "hygdata_v41.csv"
    out = []
    with open(src, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["proper"] == "Sol":
                continue
            try:
                mag = float(r["mag"])
            except ValueError:
                continue
            if mag > maglim:
                continue
            desig = ""
            if r["bayer"]:
                desig = f"{r['bayer']} {r['con']}"
            elif r["flam"]:
                desig = f"{r['flam']} {r['con']}"
            out.append([round(float(r["ra"]) * 15, 5), round(float(r["dec"]), 5), round(mag, 2),
                        r["ci"] or "", r["proper"], desig, r["hip"]])
    with gzip.open(OUT / "stars.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ra", "dec", "mag", "bv", "name", "desig", "hip"])
        w.writerows(out)
    print(f"stars.csv.gz: {len(out)} stars to mag {maglim}, {sum(1 for o in out if o[4])} with proper names")


def build_cities(cdir: Path) -> None:
    cities = json.loads((cdir / "cities.json").read_text(encoding="utf-8"))
    admin1 = {a["code"]: a["name"] for a in json.loads((cdir / "admin1.json").read_text(encoding="utf-8"))}
    with gzip.open(OUT / "cities.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "region", "country", "lat", "lon"])
        for c in cities:
            w.writerow([c["name"], admin1.get(f"{c['country']}.{c['admin1']}", ""), c["country"],
                        round(float(c["lat"]), 4), round(float(c["lng"]), 4)])
    print(f"cities.csv.gz: {len(cities)} places")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    openngc, hyg, cdir = (Path(a) for a in sys.argv[1:4])
    build_dso(openngc, hyg)
    build_stars(hyg)
    build_cities(cdir)
