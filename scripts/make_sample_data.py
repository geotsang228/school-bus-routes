"""Generate a realistic synthetic student list to test the pipeline end-to-end.

Writes data/students_2026_sample.xlsx with mixed Chinese/English headers and a
mix of full-street and estate-level addresses across 4 HK schools.
Run:  python scripts/make_sample_data.py
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from config import DATA

DISTRICTS = {
    "Sha Tin": ["Hin Keng Estate 顯徑邨", "Sun Chui Estate 新翠邨", "Mei Lam Estate 美林邨",
                "Sha Tin Centre 沙田中心", "Sui Wo Court 穗禾苑"],
    "Tai Wai": ["Festival City 名城", "Golden Lion Garden 金獅花園", "Tai Wai Village 大圍村",
                "Lung Hang Estate 隆亨邨"],
    "Ma On Shan": ["Bayshore Towers 海濤居", "Ocean View Court 海景苑", "Ma On Shan Centre 馬鞍山中心"],
    "Kowloon City": ["Symphony Bay 衙前圍道28號", "No. 60 Prince Edward Rd 太子道西60號",
                     "Kadoorie Avenue 嘉道理道", "Carpenter Rd 聯合道"],
    "Mong Kok": ["Argyle Centre 旺角中心", "Langham Place 朗豪坊", "Soy Street 豉油街",
                 "Portland Street 波特蘭街"],
    "Tsuen Wan": ["Tsuen Wan Plaza 荃灣廣場", "Luk Yeung Sun Chuen 綠楊新邨", "Riviera Gardens 海濱花園",
                  "Tsuen Tak Garden 荃德花園"],
}

SCHOOLS = {
    "Shatin Pui Ying School": ["Sha Tin", "Tai Wai", "Ma On Shan"],
    "New Territories Heep Yunn School": ["Sha Tin", "Tai Wai"],
    "Kowloon True Light Middle School": ["Kowloon City", "Mong Kok"],
    "Tsuen Wan Anglican Primary School": ["Tsuen Wan"],
}

SURNAMES = ["陳", "李", "張", "黃", "劉", "林", "周", "吳", "何", "王", "鄭", "梁", "羅", "謝", "唐", "郭", "許", "鄧", "廖", "歐陽"]
GIVEN = ["梓軒", "子晴", "俊傑", "詠欣", "嘉豪", "雅婷", "卓穎", "芷晴", "浩然", "思敏",
         "浚希", "凱琳", "文樂", "曉彤", "宇軒", "心怡", "家俊", "詩婷", "偉倫", "靜雯"]

CLASSES = ["P1A", "P1B", "P2A", "P2B", "P3A", "P3B", "S1A", "S1B", "S2A", "S2B", "S3A", "S3B"]


def main():
    rng = random.Random(42)
    rows = []
    sid = 0
    for school, districts in SCHOOLS.items():
        for _ in range(110 if "Middle" in school or "Primary" in school else 120):
            sid += 1
            dist = rng.choice(districts)
            bldg = rng.choice(DISTRICTS[dist])
            # ~60% get a full-style address (block + floor + flat), rest estate-level
            if rng.random() < 0.6:
                unit = f"{rng.randint(1, 40)}/F #{rng.randint(1, 9):02d}"
                address = f"{bldg} {unit}, {dist}"
            else:
                address = f"{bldg}, {dist}"
            rows.append({
                "學號": f"2026-{sid:04d}",
                "姓名": rng.choice(SURNAMES) + rng.choice(GIVEN),
                "School 學校": school,
                "班別": rng.choice(CLASSES),
                "住址 Address": address,
                "聯絡電話": f"{rng.choice('569')}{rng.randint(10000000, 99999999)}",
            })
    df = pd.DataFrame(rows)
    out = DATA / "students_2026_sample.xlsx"
    df.to_excel(out, index=False)
    print(f"Wrote {len(df)} students -> {out}")


if __name__ == "__main__":
    main()
