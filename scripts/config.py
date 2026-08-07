"""
School Bus Routes — config & constants.
"""
import os
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR     = PROJECT_ROOT / "data"
SCRIPTS_DIR  = PROJECT_ROOT / "scripts"
OUTPUT_DIR   = PROJECT_ROOT / "output"
ENV_FILE     = SCRIPTS_DIR / ".env"

# AMap — load from .env or env var
def _load_amap_key():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("AMAP_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("AMAP_KEY", "")

AMAP_KEY = _load_amap_key()

# Defaults
DEFAULT_SCHOOL_START  = "08:00"
DEFAULT_DISMISSAL_TIME = "15:30"   # afternoon (PM) bus departs school after this
DEFAULT_BUS_CAPACITY  = 25
WALK_RADIUS_METRES    = 500
SERVICE_TIME_PER_STOP = 1.5    # minutes per boarding/alighting
ARRIVAL_BUFFER_MINS   = 13     # arrive at school this many minutes before start (traffic buffer)
POI_SEARCH_RADIUS     = 300     # search radius for safe-stop POI lookup
WALK_RADIUS_METRES    = 200     # max walk: student home → pick-up point
HK_CENTRE_LAT         = 22.32
HK_CENTRE_LNG         = 114.17

# HK district approximate centres (lat, lng) — used by mock geocoding so
# students in the same district cluster together even without an AMap key.
DISTRICT_CENTRES = {
    "沙田":   (22.3833, 114.1883),
    "大圍":   (22.3720, 114.1780),
    "元朗":   (22.4450, 114.0300),
    "天水圍": (22.4590, 114.0030),
    "屯門":   (22.3910, 113.9730),
    "黃大仙": (22.3430, 114.2020),
    "九龍城": (22.3280, 114.1870),
    "深水埗": (22.3300, 114.1610),
    "觀塘":   (22.3120, 114.2260),
    "油尖旺": (22.3110, 114.1720),
    "荃灣":   (22.3700, 114.1100),
    "葵青":   (22.3590, 114.1280),
    "大埔":   (22.4460, 114.1660),
    "北區":   (22.4960, 114.1380),
    "西貢":   (22.3830, 114.2730),
    "離島":   (22.2620, 113.9410),
    "中西區": (22.2860, 114.1530),
    "灣仔":   (22.2780, 114.1830),
    "東區":   (22.2830, 114.2160),
    "南區":   (22.2460, 114.1600),
}
# Jitter (±degrees, ~1km) so mock points scatter realistically within a district
MOCK_JITTER_DEG = 0.008

# Geocoding
GEOCODE_BATCH_LIMIT = 100
GEOCODE_DELAY_SECS  = 0.15
AMAP_GEOCODE_URL    = "https://restapi.amap.com/v3/geocode/geo"
OSM_GEOCODE_URL     = "https://nominatim.openstreetmap.org/search"

# AMap Web Service APIs (separate permissions from geocoding)
AMAP_DRIVING_URL    = "https://restapi.amap.com/v3/direction/driving"
AMAP_POI_URL        = "https://restapi.amap.com/v3/place/around"
AMAP_STATICMAP_URL  = "https://restapi.amap.com/v3/staticmap"
AMAP_REGEO_URL      = "https://restapi.amap.com/v3/geocode/regeo"
AMAP_DISTANCE_URL   = "https://restapi.amap.com/v3/distance"

# Driving strategies (AMap v3 /direction/driving):
#   10 = avoid congestion + minimise time (AMap recommended default)
#    2 = minimise distance (shortest route)
#   14 = plan lower-cost / free-of-toll routes
DRIVING_FASTEST  = 10
DRIVING_SHORTEST = 2
DRIVING_TOLLFREE = 14

# Safe pick-up POI keywords (nearest safe stop for boarding): bus stops + coach stations
POI_SAFE_KEYWORDS = ["巴士站", "长途汽车站", "客运站", "公交车站", "大厦入口", "邨入口", "广场", "中心", "屋苑"]
# Solver
SOLVER_TIME_LIMIT_SECS = 30
