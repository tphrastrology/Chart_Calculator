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
from flatlib import const, angle
from flatlib.geopos import GeoPos
from flatlib.datetime import Datetime
from flatlib.chart import Chart
import flatlib.ephem
import swisseph as swe
import os

# ---- Ephemeris path (point to repo root or 'ephe')
flatlib.ephem.ephepath = os.getenv("EPHE_PATH", ".")
swe.set_ephe_path(flatlib.ephem.ephepath)

app = FastAPI(title="Natal Chart API", version="1.0.1")

# ---- House systems (flatlib constant names vary; these are correct)
HOUSE_MAP = {
    "Placidus":      const.HOUSES_PLACIDUS,
    "Koch":          const.HOUSES_KOCH,
    "Porphyry":      const.HOUSES_PORPHYRIUS,  # spelling in flatlib
    "Porphyrius":    const.HOUSES_PORPHYRIUS,
    "Regiomontanus": const.HOUSES_REGIOMONTANUS,
    "Campanus":      const.HOUSES_CAMPANUS,
    "Equal":         const.HOUSES_EQUAL,
    "WholeSign":     const.HOUSES_WHOLE_SIGN,
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
        if key not in HOUSE_MAP:
            raise ValueError(f"house_system must be one of: {', '.join(HOUSE_MAP.keys())}")
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

        # 2) Build a Chart WITHOUT objects to avoid flatlib ephem calls
        #    IDs=[] prevents Chart() from preloading planetary objects (the source of tuple errors)
        fl_dt = Datetime(utc_dt.strftime("%Y/%m/%d"), utc_dt.strftime("%H:%M"), "+00:00")
        pos = GeoPos(lat=payload.latitude, lon=payload.longitude)
        chart = Chart(fl_dt, pos, IDs=[], hsys=HOUSE_MAP[payload.house_system])

        # 3) Angles & houses are safe via flatlib
        asc = angle.ASC(chart)
        mc  = angle.MC(chart)
        houses = []
        for i in range(1, 13):
            cusp = chart.houses.getHouse(i)
            s, d = lon_to_sign_deg(cusp.lon)
            houses.append({"n": i, "sign": s, "deg": d, "lon": round(cusp.lon % 360.0, 4)})

        # 4) Planet longitudes via pyswisseph (robust & independent of flatlib objects)
        jdut = swe.julday(
            int(utc_dt.strftime("%Y")),
            int(utc_dt.strftime("%m")),
            int(utc_dt.strftime("%d")),
            int(utc_dt.strftime("%H")) + int(utc_dt.strftime("%M"))/60.0,
        )

        planets = []
        body_lons = {}
        for label in PLANET_LABELS:
            try:
                lon, lat = swe_calc_lonlat(jdut, SWE_IDS[label])
                s, d = lon_to_sign_deg(lon)
                body_lons[label] = lon
                planets.append({
                    "name": label, "sign": s, "deg": d,
                    "lon": round(lon, 4), "lat": round(lat, 4), "speed": 0.0
                })
            except Exception as e:
                planets.append({"name": label, "error": f"calc failed: {e}"})

        # Nodes (Mean if present, else True) + South Node
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

        # 5) Aspects using computed longitudes (no flatlib.get calls)
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
        asc_obj = angle_to_obj("ASC", asc.lon)
        mc_obj  = angle_to_obj("MC",  mc.lon)
        return {
            "meta": {
                "house_system": payload.house_system,
                "datetime_utc": utc_iso,
                "location": {"lat": payload.latitude, "lng": payload.longitude},
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

