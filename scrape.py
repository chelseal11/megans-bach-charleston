"""
Scrape Airbnb listings for Charleston bachelorette trip.
Writes to listings.json incrementally so no progress is lost.

Usage:
  pip install pyairbnb
  python scrape.py
"""

import pyairbnb
import pyairbnb.details as airbnb_details
import json
import math
import time
import re
import os

# ─── CONFIG ───
CHECK_IN = "2026-07-16"
CHECK_OUT = "2026-07-19"
NIGHTS = 3
GUESTS = 10
OUTPUT_FILE = "listings.json"

# Charleston, SC bounding box (peninsula + surrounding neighborhoods)
NE_LAT, NE_LNG = 32.850, -79.850
SW_LAT, SW_LNG = 32.720, -80.020
ZOOM = 12

# Upper King Street — the bachelorette bullseye
UPPER_KING_LAT = 32.7876
UPPER_KING_LNG = -79.9378

# Bach-relevant amenities to flag
BACH_AMENITIES = {
    "pool", "hot tub", "outdoor shower", "patio", "balcony", "deck",
    "rooftop", "bbq", "grill", "fire pit", "game room", "piano",
    "sound system", "bluetooth", "wifi", "kitchen", "full kitchen",
    "washer", "dryer", "free parking", "ev charger", "gym",
}


def parse_number(text):
    """Extract first number from a string like '4 bedrooms'."""
    match = re.search(r"[\d.]+", str(text))
    return float(match.group()) if match else 0


def haversine_miles(lat1, lng1, lat2, lng2):
    """Distance in miles between two lat/lng points."""
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def score_location(lat, lng):
    """Score 1-10 based on distance to Upper King Street."""
    miles = haversine_miles(lat, lng, UPPER_KING_LAT, UPPER_KING_LNG)
    if miles <= 0.3:   return 10  # walking distance
    if miles <= 0.5:   return 9
    if miles <= 0.75:  return 8
    if miles <= 1.0:   return 7   # short Uber
    if miles <= 1.5:   return 6
    if miles <= 2.0:   return 5
    if miles <= 3.0:   return 4
    return 3


def score_value(price_per_night):
    """Score 1-10 purely on price per person per night. Cheaper = better."""
    ppn = price_per_night / GUESTS if price_per_night else 999

    if ppn <= 40:     return 10
    elif ppn <= 60:   return 9
    elif ppn <= 80:   return 8
    elif ppn <= 100:  return 7
    elif ppn <= 130:  return 6
    elif ppn <= 170:  return 5
    elif ppn <= 220:  return 4
    elif ppn <= 300:  return 3
    else:             return 2


def score_bach_fit(max_guests, bedrooms, bathrooms, amenities_lower):
    """Score 1-10 for bachelorette suitability based on amenities + capacity."""
    score = 3.0  # baseline

    # Can it actually fit 10?
    if max_guests >= 12:   score += 1.0
    elif max_guests >= 10: score += 0.5
    else:                  score -= 1.0

    # Bedrooms — enough space to spread out?
    if bedrooms >= 6:   score += 0.5
    elif bedrooms >= 4: score += 0.25
    elif bedrooms <= 2: score -= 0.5

    # Bathrooms — critical with 10 people getting ready together
    if bathrooms >= 5:   score += 1.0
    elif bathrooms >= 4: score += 0.75
    elif bathrooms >= 3: score += 0.5
    elif bathrooms <= 1: score -= 1.0

    # Fun / party amenities
    fun_keywords = ["pool", "hot tub", "rooftop", "fire pit", "game room",
                    "patio", "deck", "balcony", "grill", "bbq", "outdoor",
                    "sound system", "bluetooth", "piano", "gym"]
    fun_count = sum(1 for kw in fun_keywords if any(kw in a for a in amenities_lower))
    score += min(fun_count * 0.4, 2.5)

    # Practical amenities for a group trip
    practical_keywords = ["kitchen", "washer", "dryer", "parking",
                          "coffee", "dishwasher", "iron"]
    practical_count = sum(1 for kw in practical_keywords if any(kw in a for a in amenities_lower))
    score += min(practical_count * 0.25, 1.5)

    return max(1, min(10, round(score, 1)))


def save_progress(listings):
    """Write current listings to JSON file."""
    with open(OUTPUT_FILE, "w") as f:
        json.dump(listings, f, indent=2)


def main():
    print("Searching Airbnb for Charleston, SC...")
    print(f"  Dates: {CHECK_IN} to {CHECK_OUT}")
    print(f"  Guests: {GUESTS}")
    print()

    # Step 1: Search all pages
    results = pyairbnb.search_all(
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        ne_lat=NE_LAT,
        ne_long=NE_LNG,
        sw_lat=SW_LAT,
        sw_long=SW_LNG,
        zoom_value=ZOOM,
        currency="USD",
        language="en",
        adults=GUESTS,
        price_min=0,
        price_max=0,
        place_type="",
    )

    print(f"Found {len(results)} listings from search.\n")

    # Load existing progress if any
    enriched = []
    seen_ids = set()
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            enriched = json.load(f)
            seen_ids = {l["room_id"] for l in enriched}
            print(f"Resuming — {len(enriched)} already scraped, skipping those.\n")

    # Step 2: Enrich each listing with details + scores
    for i, listing in enumerate(results):
        room_id = str(listing["room_id"])

        if room_id in seen_ids:
            continue

        print(f"[{len(enriched) + 1}/{len(results)}] {listing.get('name', room_id)}...")

        try:
            # Call details.get directly to bypass broken price.get in the library
            room_url = f"https://www.airbnb.com/rooms/{room_id}"
            detail_data, _price_input, _cookies = airbnb_details.get(room_url, "en", "")

            # Parse bedrooms/bathrooms from sub_description
            sub_items = detail_data.get("sub_description", {}).get("items", [])
            bedrooms_str = next((s for s in sub_items if "bedroom" in s.lower()), "0")
            bathrooms_str = next((s for s in sub_items if "bath" in s.lower()), "0")
            bedrooms = parse_number(bedrooms_str)
            bathrooms = parse_number(bathrooms_str)
            max_guests = detail_data.get("person_capacity", 0)

            # Collect all amenities
            amenities = []
            for group in detail_data.get("amenities", []):
                for a in group.get("values", []):
                    if a.get("available", True):
                        amenities.append(a["title"])
            amenities_lower = [a.lower() for a in amenities]

            # Collect photo URLs for carousel — search images + detail photos
            photo_urls = [img["url"] for img in listing.get("images", []) if img.get("url")]
            for photo in detail_data.get("photos", []):
                url = photo.get("large_url") or photo.get("url") or photo.get("picture")
                if url and url not in photo_urls:
                    photo_urls.append(url)

            # Coordinates (library has "longitud" typo in search results)
            coords = listing.get("coordinates", {})
            lat = coords.get("latitude", 0)
            lng = coords.get("longitud") or coords.get("longitude", 0)

            # Price (from search results — already accurate)
            price_info = listing.get("price", {})
            price_per_night = price_info.get("unit", {}).get("amount", 0)
            total_cost = price_info.get("total", {}).get("amount", 0)
            rating = listing.get("rating", 0)
            reviews = listing.get("reviews_count", 0)

            # ─── SCORING ───
            loc = score_location(lat, lng)
            val = score_value(price_per_night)
            bach = score_bach_fit(max_guests, bedrooms, bathrooms, amenities_lower)
            total_score = round(loc * 0.34 + val * 0.33 + bach * 0.33, 2)

            entry = {
                "room_id": room_id,
                "name": listing.get("name", ""),
                "price_per_night": price_per_night,
                "total_cost": total_cost,
                "price_per_person_per_night": round(price_per_night / GUESTS, 2),
                "lat": lat,
                "lng": lng,
                "bedrooms": bedrooms,
                "bathrooms": bathrooms,
                "max_guests": max_guests,
                "amenities": amenities,
                "photo_urls": photo_urls[:15],  # cap at 15
                "rating": rating,
                "reviews": reviews,
                "score_location": loc,
                "score_value": val,
                "score_bach_fit": bach,
                "score_total": total_score,
                "airbnb_url": f"https://www.airbnb.com/rooms/{room_id}",
            }

            enriched.append(entry)
            seen_ids.add(room_id)

            # Save after every listing
            save_progress(enriched)
            print(f"  ✓ loc={loc} val={val} bach={bach} total={total_score}")

            time.sleep(1.5)

        except Exception as e:
            print(f"  ✗ Error: {e}")
            time.sleep(2)

    save_progress(enriched)
    print(f"\nDone! {len(enriched)} listings saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
