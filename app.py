# Chart_Calculator — Free and simple astrological natal chart API
# Copyright (C) 2025  TPHR Astrology
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from datetime import datetime
from dateutil import tz
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from typing import Optional

# ---- Libraries
# We keep flatlib only for constants if you want, but angles/houses now use pyswisseph directly.
from flatlib import const  # still referenced for future-proofing, but not required for angles/houses
import flatlib.ephem
from flatlib.datetime import Datetime  # optional (not used once we switch fully to swe)
import swisseph as swe
import os

app = FastAPI(title="Natal Chart API", version="1.0.2")

# ---- Ephemeris path (point to repo root or 'ephe')
flatlib.ephem.ephepath = os.getenv("EPHE_PATH", ".")
swe.set_ephe_path(flatlib.ephem.ephepath)

# ---- House system mapping for Swiss Ephemeris
# Swiss letters: P=Placidus, K=Koch, O=Porphyry, R=Regiomontanus, C=Campanus, E=Equal, W=Whole Sign
HSYS_LETTER = {
    "Placidus":      b"P",
    "Koch":          b"K",
    "Porphyry":      b"O",
    "Porphyrius":    b"O",
    "Regiomontanus": b"R",
    "Campanus":      b"C",
    "Equal":         b"E",
    "WholeSign":     b"W",
}

# ---- Signs + helpers
SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]

def lon_to_sign_deg(lon: float):
    lon = lon % 360.0
    sign_index = int(lon // 30)
    deg_in_sign = lon - sign_index * 30
    return SIGNS[sign_index], round(deg_in_sign, 2)

def angle_to_obj(name: str, lon: float):
    sign, deg = lon_to_sign_deg(lon)
    return {"name": name, "sign": sign, "deg": deg, "lon": round(lon % 360.0, 4)}

# ---- Planet labels and aspect definitions
PLANET_LABELS = [
    "Sun","Moon","Mercury","Venus","Mars","Jupiter","Saturn","Uranus","Neptune","Pluto"
]
ASPECTS = [
    ("conjunction", 0,   8),
    ("opposition",  180, 8),
    ("trine",       120, 7),
    ("square",      90,  6),
    ("sextile",     60,  5),
]

# ---- Robust pyswisseph calculator
# Tries Swiss ephemeris with speed, then without, then Moshier fallback.
# Always returns (lon, lat).

def swe_calc_lonlat(jdut: float, planet_id: int):
    try:
        vals, _flags = swe.calc_ut(jdut, planet_id, swe.FLG_SWIEPH | swe.FLG_SPEED)
        if isinstance(vals, (list, tuple)) and len(vals) >= 2:
            return vals[0] % 360.0, vals[1]
    except Exception:
        pass
    try:
        vals, _flags = swe.calc_ut(jdut, planet_id, swe.FLG_SWIEPH)
        if isinstance(vals, (list, tuple)) and len(vals) >= 2:
            return vals[0] % 360.0, vals[1]
    except Exception:
        pass
    vals, _flags = swe.calc_ut(jdut, planet_id, swe.FLG_MOSEPH)
    if not (isinstance(vals, (list, tuple)) and len(vals) >= 2):
        raise RuntimeError("pyswisseph returned invalid tuple")
    return vals[0] % 360.0, vals[1]

SWE_IDS = {
    "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY, "Venus": swe.VENUS,
    "Mars": swe.MARS, "Jupiter": swe.JUPITER, "Saturn": swe.SATURN,
    "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE, "Pluto": swe.PLUTO,
}

# ---- Request model
ALIASES = {
    "porphyry": "Porphyry",
    "porphyrius": "Porphyrius",
    "wholesign": "WholeSign",
    "whole sign": "WholeSign",
    "whole-sign": "WholeSign",
}

class NatalInput(BaseModel):
    date: str = Field(..., example="1990-06-12")
    time: str = Field(..., example="14:23")
    timezone: str = Field(..., example="America/New_York")
    latitude: float
    longitude: float
    house_system: Optional[str] = Field(default="Placidus")

    @field_validator("house_system")
    @classmethod
    def valid_house(cls, v):
        key = ALIASES.get(str(v).strip().lower(), v)
        if key not in HSYS_LETTER:
            raise ValueError(f"house_system must be one of: {', '.join(HSYS_LETTER.keys())}")
        return key

# ---- Time helper

def to_utc_iso(date_str, time_str, tzname):
    local_tz = tz.gettz(tzname)
    if not local_tz:
        raise ValueError("Invalid timezone string. Use IANA, e.g., 'America/New_York'.")
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    local_dt = naive.replace(tzinfo=local_tz)
    utc_dt = local_dt.astimezone(tz.UTC)
    return utc_dt, utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

# ---- Endpoint

@app.post("/natal")
def natal(payload: NatalInput, request: Request):
    try:
        # 1) Normalize to UTC
        utc_dt, utc_iso = to_utc_iso(payload.date, payload.time, payload.timezone)

        # 2) Julian Day and basic inputs
        jdut = swe.julday(
            int(utc_dt.strftime("%Y")),
            int(utc_dt.strftime("%m")),
            int(utc_dt.strftime("%d")),
            int(utc_dt.strftime("%H")) + int(utc_dt.strftime("%M"))/60.0,
        )
        lat = payload.latitude
        lon = payload.longitude

        # 3) Houses & angles via Swiss Ephemeris
        hsys = HSYS_LETTER[payload.house_system]
        cusps, ascmc = swe.houses_ex(jdut, swe.FLG_SWIEPH, lat, lon, hsys)
        # ascmc: [Asc, MC, ARMC, vertex, EquAsc, co-Asc (W), co-Asc (M), polar Asc]
        asc_lon = ascmc[0]
        mc_lon  = ascmc[1]

        houses = []
        # pyswisseph returns cusps as a list-like of length 13 where index 1..12 are valid
        for i in range(1, 13):
            cusp_lon = cusps[i]
            s, d = lon_to_sign_deg(cusp_lon)
            houses.append({"n": i, "sign": s, "deg": d, "lon": round(cusp_lon % 360.0, 4)})

        # 4) Planets via pyswisseph (robust)
        planets = []
        body_lons = {}
        for label in PLANET_LABELS:
            try:
                lon_p, lat_p = swe_calc_lonlat(jdut, SWE_IDS[label])
                s, d = lon_to_sign_deg(lon_p)
                body_lons[label] = lon_p
                planets.append({
                    "name": label, "sign": s, "deg": d,
                    "lon": round(lon_p, 4), "lat": round(lat_p, 4), "speed": 0.0
                })
            except Exception as e:
                planets.append({"name": label, "error": f"calc failed: {e}"})

        # Nodes (Mean if available, else True) + South Node
        try:
            node_pid = getattr(swe, "MEAN_NODE", getattr(swe, "TRUE_NODE"))
            node_lon, _ = swe_calc_lonlat(jdut, node_pid)
            n_sign, n_deg = lon_to_sign_deg(node_lon)
            body_lons["North Node"] = node_lon
            planets.append({
                "name": "North Node", "sign": n_sign, "deg": n_deg,
                "lon": round(node_lon, 4), "lat": 0.0, "speed": 0.0
            })
            south_lon = (node_lon + 180.0) % 360.0
            s_sign, s_deg = lon_to_sign_deg(south_lon)
            body_lons["South Node"] = south_lon
            planets.append({
                "name": "South Node", "sign": s_sign, "deg": s_deg,
                "lon": round(south_lon, 4), "lat": 0.0, "speed": 0.0
            })
        except Exception:
            pass

        # 5) Aspects using computed longitudes
        names_for_aspects = [n for n in PLANET_LABELS + ["North Node","South Node"] if n in body_lons]
        asp_results = []
        for i, na in enumerate(names_for_aspects):
            lonA = body_lons[na] % 360.0
            for nb in names_for_aspects[i+1:]:
                lonB = body_lons[nb] % 360.0
                dist = min(abs(lonA - lonB), 360.0 - abs(lonA - lonB))
                for name, exact, orb in ASPECTS:
                    diff = abs(dist - exact)
                    if diff <= orb:
                        asp_results.append({
                            "a": na, "b": nb, "type": name,
                            "orb": round(diff, 2), "dist": round(dist, 2), "exact": exact
                        })
                        break

        # 6) Response
        asc_obj = angle_to_obj("ASC", asc_lon)
        mc_obj  = angle_to_obj("MC",  mc_lon)
        return {
            "meta": {
                "house_system": payload.house_system,
                "datetime_utc": utc_iso,
                "location": {"lat": lat, "lng": lon},
            },
            "angles": {"ASC": asc_obj, "MC": mc_obj},
            "houses": houses,
            "planets": planets,
            "aspects": asp_results,
            "rising_sign": {"sign": asc_obj["sign"], "deg": asc_obj["deg"]},
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Calculation error: {e}")

@app.get("/healthz")
def health():
    return {"ok": True}


