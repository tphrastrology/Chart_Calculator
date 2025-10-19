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
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from typing import Optional

# flatlib / Swiss Ephemeris
from flatlib import const, angle
from flatlib.geopos import GeoPos
from flatlib.datetime import Datetime
from flatlib.chart import Chart
import swisseph as swe
import flatlib.ephem
flatlib.ephem.ephepath = "."      # or "ephe" if that’s where your files are
swe.set_ephe_path(flatlib.ephem.ephepath)

# pyswisseph-based calculator with robust fallbacks
def swe_calc_lonlat(jdut, planet_id):
    """Return (lon, lat) using Swiss ephemeris; fallback to Moshier if needed."""
    # 1) Try Swiss ephemeris with speed flag (safe default)
    try:
        vals, flags = swe.calc_ut(jdut, planet_id, swe.FLG_SWIEPH | swe.FLG_SPEED)
        if isinstance(vals, (list, tuple)) and len(vals) >= 2:
            return vals[0] % 360.0, vals[1]
    except Exception:
        pass
    # 2) Fallback to Swiss ephemeris without speed
    try:
        vals, flags = swe.calc_ut(jdut, planet_id, swe.FLG_SWIEPH)
        if isinstance(vals, (list, tuple)) and len(vals) >= 2:
            return vals[0] % 360.0, vals[1]
    except Exception:
        pass
    # 3) Last resort: Moshier (no .se1 files needed)
    vals, flags = swe.calc_ut(jdut, planet_id, swe.FLG_MOSEPH)
    if not (isinstance(vals, (list, tuple)) and len(vals) >= 2):
        raise RuntimeError("pyswisseph returned invalid tuple")
    return vals[0] % 360.0, vals[1]


def pt_with_fallback(name):
    """Return planet dict; fallback via swe.calc_ut for Saturn/Uranus if flatlib chokes."""
    try:
        p = chart.get(name)
        s, d = lon_to_sign_deg(p.lon)
        return {
            "name": p.body,
            "sign": s, "deg": d,
            "lon": round(p.lon % 360.0, 4),
            "lat": round(getattr(p, "lat", 0.0), 4),
            "speed": round(getattr(p, "speed", 0.0), 5)
        }
    except Exception:
        # fallback only for Saturn/Uranus
        name_map = {
            getattr(const, "SATURN"):  swe.SATURN,
            getattr(const, "URANUS"):  swe.URANUS,
        }
        if name not in name_map:
            raise  # rethrow for others

        # compute via pyswisseph
        swe.set_ephe_path(flatlib.ephem.ephepath)
        jdut = swe.julday(
            int(utc_dt.strftime("%Y")),
            int(utc_dt.strftime("%m")),
            int(utc_dt.strftime("%d")),
            int(utc_dt.strftime("%H")) + int(utc_dt.strftime("%M"))/60.0
        )
        vals, _flags = swe.calc_ut(jdut, name_map[name])  # (lon, lat, dist, …)
        lon = vals[0] % 360.0
        s, d = lon_to_sign_deg(lon)
        label = "Saturn" if name == getattr(const, "SATURN") else "Uranus"
        return {
            "name": label,
            "sign": s, "deg": d,
            "lon": round(lon, 4),
            "lat": round(vals[1], 4) if len(vals) > 1 else 0.0,
            "speed": 0.0  # leave 0.0; speed via fallback is optional
        }

app = FastAPI(title="Natal Chart API", version="1.0.0")

HOUSE_MAP = {
    "Placidus":      const.HOUSES_PLACIDUS,
    "Koch":          const.HOUSES_KOCH,
    "Porphyry":      const.HOUSES_PORPHYRIUS,     # correct constant
    "Porphyrius":    const.HOUSES_PORPHYRIUS,     # accept either label
    "Regiomontanus": const.HOUSES_REGIOMONTANUS,
    "Campanus":      const.HOUSES_CAMPANUS,
    "Equal":         const.HOUSES_EQUAL,
    "WholeSign":     const.HOUSES_WHOLE_SIGN      # <-- correct spelling
}

# ---- Node ID selection (handles different flatlib versions) ----
# We'll pick whichever lunar node constant exists in this flatlib build.
for _attr in ("MEAN_NODE", "TRUE_NODE", "NORTH_NODE"):
    if hasattr(const, _attr):
        NODE_ID = getattr(const, _attr)
        break
else:
    raise ImportError("No lunar node constant (MEAN/TRUE/NORTH) found in flatlib.const")

# Use the chosen node ID; we'll compute South Node ourselves later.
PLANET_LIST = [
  const.SUN, const.MOON,
  const.MERCURY, const.VENUS, const.MARS,
  const.JUPITER, const.SATURN
]


# Aspect definitions: (name, exact_degrees, max_orb_degrees)
ASPECTS = [
    ("conjunction", 0,   8),
    ("opposition",  180, 8),
    ("trine",       120, 7),
    ("square",      90,  6),
    ("sextile",     60,  5),
]

SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]

def lon_to_sign_deg(lon):
    lon = lon % 360.0
    sign_index = int(lon // 30)
    deg_in_sign = lon - sign_index * 30
    return SIGNS[sign_index], round(deg_in_sign, 2)

def angle_to_obj(name, lon):
    sign, deg = lon_to_sign_deg(lon)
    return {"name": name, "sign": sign, "deg": deg, "lon": round(lon % 360.0, 4)}

class NatalInput(BaseModel):
    date: str = Field(..., example="1990-06-12")          # YYYY-MM-DD
    time: str = Field(..., example="14:23")               # 24h HH:MM
    timezone: str = Field(..., example="America/New_York")# IANA timezone
    latitude: float
    longitude: float
    house_system: Optional[str] = Field(default="Placidus")

    @field_validator("house_system")
    @classmethod
    def valid_house(cls, v):
        if v not in HOUSE_MAP:
            raise ValueError(f"house_system must be one of: {', '.join(HOUSE_MAP.keys())}")
        return v

ALIASES = {
    "porphyry": "Porphyry",
    "porphyrius": "Porphyrius",
    "wholesign": "WholeSign",
    "whole sign": "WholeSign",
    "whole-sign": "WholeSign",
}

@field_validator("house_system")
@classmethod
def valid_house(cls, v):
    key = ALIASES.get(str(v).strip().lower(), v)
    if key not in HOUSE_MAP:
        raise ValueError(f"house_system must be one of: {', '.join(HOUSE_MAP.keys())}")
    return key


def to_utc_iso(date_str, time_str, tzname):
    local_tz = tz.gettz(tzname)
    if not local_tz:
        raise ValueError("Invalid timezone string. Use IANA, e.g., 'America/New_York'.")
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    local_dt = naive.replace(tzinfo=local_tz)
    utc_dt = local_dt.astimezone(tz.UTC)
    return utc_dt, utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

@app.post("/natal")
def natal(payload: NatalInput):
    try:
        # 1) Normalize to UTC
        utc_dt, utc_iso = to_utc_iso(payload.date, payload.time, payload.timezone)

        # 2) Build flatlib objects
        fl_dt = Datetime(utc_dt.strftime("%Y/%m/%d"), utc_dt.strftime("%H:%M"), "+00:00")
        pos = GeoPos(lat=payload.latitude, lon=payload.longitude)

        chart = Chart(fl_dt, pos, hsys=HOUSE_MAP[payload.house_system])  # no IDs preload

        # 3) Angles
        asc = angle.ASC(chart)
        mc  = angle.MC(chart)

        # 4) Houses
        houses = []
        for i in range(1, 13):
            cusp = chart.houses.getHouse(i)
            s, d = lon_to_sign_deg(cusp.lon)
            houses.append({"n": i, "sign": s, "deg": d, "lon": round(cusp.lon % 360.0, 4)})

        # 5) Planets
        # --- planets via pyswisseph to avoid flatlib tuple issues ---
        # Build Julian Day in UTC for pyswisseph
        jdut = swe.julday(
            int(utc_dt.strftime("%Y")),
            int(utc_dt.strftime("%m")),
            int(utc_dt.strftime("%d")),
            int(utc_dt.strftime("%H")) + int(utc_dt.strftime("%M"))/60.0
        )
        
        planet_labels = ["Sun","Moon","Mercury","Venus","Mars","Jupiter","Saturn","Uranus","Neptune","Pluto"]
        planets = []
        
        for label in planet_labels:
            try:
                lon, lat = swe_calc_lonlat(jdut, SWE_IDS[label])
                s, d = lon_to_sign_deg(lon)
                planets.append({
                    "name": label,
                    "sign": s,
                    "deg": d,
                    "lon": round(lon, 4),
                    "lat": round(lat, 4),
                    "speed": 0.0  # omit true speed to keep things simple/stable
                })
            except Exception as e:
                # If any single body fails, skip it but keep the API responsive
                planets.append({
                    "name": label,
                    "error": f"calc failed: {e}"
                })
        
        # Node + South Node (derived)
        try:
            # Use mean node if available in your flatlib build; otherwise compute via pyswisseph
            if 'NODE_ID' in globals():
                node_obj = chart.get(NODE_ID)
                node_lon = node_obj.lon % 360.0
            else:
                node_lon, _ = swe_calc_lonlat(jdut, getattr(swe, "MEAN_NODE", swe.TRUE_NODE))
            n_sign, n_deg = lon_to_sign_deg(node_lon)
            planets.append({
                "name": "North Node",
                "sign": n_sign, "deg": n_deg,
                "lon": round(node_lon, 4), "lat": 0.0, "speed": 0.0
            })
            south_lon = (node_lon + 180.0) % 360.0
            s_sign, s_deg = lon_to_sign_deg(south_lon)
            planets.append({
                "name": "South Node",
                "sign": s_sign, "deg": s_deg,
                "lon": round(south_lon, 4), "lat": 0.0, "speed": 0.0
            })
        except Exception:
            pass

        # 6) Aspects (degree-based, using the longitudes we already computed)
        # Build a {name: lon} map from the planets list we just created
        body_lons = {p["name"]: p.get("lon") for p in planets if "lon" in p}
        
        # include nodes if present
        if any(p.get("name") == "North Node" for p in planets):
            body_lons["North Node"] = next(p["lon"] for p in planets if p["name"] == "North Node")
        if any(p.get("name") == "South Node" for p in planets):
            body_lons["South Node"] = next(p["lon"] for p in planets if p["name"] == "South Node")
        
        names_for_aspects = [
            "Sun","Moon","Mercury","Venus","Mars","Jupiter","Saturn","Uranus","Neptune","Pluto",
            "North Node","South Node"
        ]
        names_for_aspects = [n for n in names_for_aspects if n in body_lons]
        
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
                            "a": na,
                            "b": nb,
                            "type": name,
                            "orb": round(diff, 2),
                            "dist": round(dist, 2),
                            "exact": exact
                        })
                        break


        # 7) Response
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
            "rising_sign": {"sign": asc_obj["sign"], "deg": asc_obj["deg"]} 
        }

    except Exception as e:
        # Surface a readable error in the API instead of a generic 500
        raise HTTPException(status_code=500, detail=f"Calculation error: {e}")

SWE_IDS = {
    "Sun": swe.SUN,
    "Moon": swe.MOON,
    "Mercury": swe.MERCURY,
    "Venus": swe.VENUS,
    "Mars": swe.MARS,
    "Jupiter": swe.JUPITER,
    "Saturn": swe.SATURN,
    "Uranus": swe.URANUS,
    "Neptune": swe.NEPTUNE,
    "Pluto": swe.PLUTO,
}

@app.get("/healthz")
def health():
    return {"ok": True}
