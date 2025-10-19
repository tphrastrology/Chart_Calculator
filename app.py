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

# Minimal, robust FastAPI app that uses ONLY swe.houses (no houses_ex) for angles/cusps
# and uses pyswisseph for planet longitudes. Avoids flatlib entirely to sidestep
# any version-specific wrappers that caused 'tuple index out of range' earlier.

# Ultra-compat version: forces byte house code, guards all tuple shapes, never indexes blindly.
# Drop in as app.py

from datetime import datetime
from dateutil import tz
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field, field_validator
from typing import Optional
import swisseph as swe
import os

# ---- Config / Secrets
API_KEY = os.getenv("API_KEY")
EPHE_PATH = os.getenv("EPHE_PATH", ".")
swe.set_ephe_path(EPHE_PATH)

app = FastAPI(title="Natal Chart API", version="1.0.7-ultra-fixed")

# ---- House codes (Swiss Ephemeris expects a 1-byte code)
HSYS_CHAR = {
    "Placidus":      b"P",
    "Koch":          b"K",
    "Porphyry":      b"O",
    "Porphyrius":    b"O",
    "Regiomontanus": b"R",
    "Campanus":      b"C",
    "Equal":         b"E",
    "WholeSign":     b"W",
}

SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]

PLANET_LABELS = [
    "Sun","Moon","Mercury","Venus","Mars","Jupiter","Saturn","Uranus","Neptune","Pluto"
]
SWE_IDS = {
    "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY, "Venus": swe.VENUS,
    "Mars": swe.MARS, "Jupiter": swe.JUPITER, "Saturn": swe.SATURN,
    "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE, "Pluto": swe.PLUTO,
}
ASPECTS = [
    ("conjunction", 0,   8),
    ("opposition",  180, 8),
    ("trine",       120, 7),
    ("square",      90,  6),
    ("sextile",     60,  5),
]

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
        key = str(v).strip()
        if key not in HSYS_CHAR:
            # keys are plain strings; don't .decode()
            raise ValueError(
                "house_system must be one of: " + ", ".join(HSYS_CHAR.keys())
            )
        return key


def lon_to_sign_deg(lon: float):
    lon = lon % 360.0
    sign_index = int(lon // 30)
    deg_in_sign = lon - sign_index * 30
    return SIGNS[sign_index], round(deg_in_sign, 2)


def to_utc_iso(date_str, time_str, tzname):
    local_tz = tz.gettz(tzname)
    if not local_tz:
        raise ValueError("Invalid timezone string. Use IANA, e.g., 'America/New_York'.")
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    local_dt = naive.replace(tzinfo=local_tz)
    utc_dt = local_dt.astimezone(tz.UTC)
    return utc_dt, utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def swe_calc_lonlat(jdut: float, planet_id: int):
    # Strictly validate tuple shape at each step
    def ok(x):
        return isinstance(x, (list, tuple)) and len(x) >= 2
    try:
        vals, _ = swe.calc_ut(jdut, planet_id, swe.FLG_SWIEPH | swe.FLG_SPEED)
        if ok(vals):
            return vals[0] % 360.0, vals[1]
    except Exception:
        pass
    try:
        vals, _ = swe.calc_ut(jdut, planet_id, swe.FLG_SWIEPH)
        if ok(vals):
            return vals[0] % 360.0, vals[1]
    except Exception:
        pass
    vals, _ = swe.calc_ut(jdut, planet_id, swe.FLG_MOSEPH)
    if not ok(vals):
        raise RuntimeError(f"pyswisseph returned invalid tuple for planet id {planet_id}")
    return vals[0] % 360.0, vals[1]


@app.post("/natal")
def natal(payload: NatalInput, x_api_key: Optional[str] = Header(default=None)):
    # Simple header auth
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        # UTC & Julian Day
        utc_dt, utc_iso = to_utc_iso(payload.date, payload.time, payload.timezone)
        jdut = swe.julday(
            int(utc_dt.strftime("%Y")),
            int(utc_dt.strftime("%m")),
            int(utc_dt.strftime("%d")),
            int(utc_dt.strftime("%H")) + int(utc_dt.strftime("%M"))/60.0,
        )
        lat = float(payload.latitude)
        lon = float(payload.longitude)

        # Houses & angles — pass a 1-byte house code (our dict already stores bytes)
        hsys = HSYS_CHAR[payload.house_system]
        cusps_raw, ascmc = swe.houses(jdut, lat, lon, hsys)
        if not (isinstance(ascmc, (list, tuple)) and len(ascmc) >= 2):
            raise RuntimeError("houses() did not return ASC/MC as expected")
        asc_lon = float(ascmc[0])
        mc_lon  = float(ascmc[1])

        # Normalize cusp array defensively
        if isinstance(cusps_raw, (list, tuple)):
            L = len(cusps_raw)
            if L == 13:
                cusps = [float(cusps_raw[i]) for i in range(1, 13)]
            elif L >= 12:
                cusps = [float(cusps_raw[i]) for i in range(0, 12)]
            else:
                raise RuntimeError(f"Unexpected cusps length: {L}")
        else:
            raise RuntimeError("houses() cusps not list/tuple")

        houses = []
        for i in range(12):
            cusp_lon = cusps[i]
            s, d = lon_to_sign_deg(cusp_lon)
            houses.append({"n": i+1, "sign": s, "deg": d, "lon": round(cusp_lon % 360.0, 4)})

        # Planets
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

        # Nodes
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

        # Aspects
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

        # Format angles
        asc_sign, asc_deg = lon_to_sign_deg(asc_lon)
        mc_sign, mc_deg   = lon_to_sign_deg(mc_lon)

        return {
            "meta": {
                "house_system": payload.house_system,
                "datetime_utc": utc_iso,
                "location": {"lat": lat, "lng": lon},
            },
            "angles": {"ASC": {"name":"ASC","sign":asc_sign,"deg":asc_deg,"lon":round(asc_lon,4)},
                        "MC":  {"name":"MC","sign":mc_sign,"deg":mc_deg,"lon":round(mc_lon,4)}},
            "houses": houses,
            "planets": planets,
            "aspects": asp_results,
            "rising_sign": {"sign": asc_sign, "deg": asc_deg},
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Calculation error: {e}")


@app.get("/healthz")
def health():
    return {"ok": True}
