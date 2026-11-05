"""
Airbnb listings + reviews: cleaning script for the Power BI project
Data: Maven Analytics / Inside Airbnb (10 cities)

Put Listings.csv and Reviews.csv in the same folder as this file (or one folder
below it) and run it. The cleaned tables go into an "airbnb_clean" folder next to
the script. It takes a few minutes because Reviews.csv is big.

Output tables
    fact_reviews.csv           one row per review (~5.37M)
    dim_listing.csv            one row per listing (~280K)
    dim_host.csv               one row per host (~182K)
    dim_city.csv               one row per city (10)
    dim_date.csv               one row per day
    neighbourhood_summary.csv  one row per neighbourhood (for the map)
"""
import json
import os
import re

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- settings
HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.environ.get("AIRBNB_INPUT_DIR", HERE)
OUT_DIR = os.environ.get("AIRBNB_OUT", os.path.join(HERE, "airbnb_clean"))

# every city was scraped on a different day in Feb/Mar 2021, so Feb 2021 is incomplete.
# Jan 2021 is the last month that is complete for all ten cities.
SNAPSHOT_DATE = pd.Timestamp("2021-03-01")
LAST_FULL_MONTH_END = pd.Timestamp("2021-01-31")
COVID_START = pd.Timestamp("2020-03-01")

# prices in the file are in local currency, so I convert everything to USD
# using the ECB reference rates for 26 Feb 2021 (units of currency per 1 EUR)
FX_DATE = "2021-02-26"
ECB_PER_EUR = {"EUR": 1.0, "USD": 1.2121, "AUD": 1.5605, "BRL": 6.6644, "TRY": 9.0168,
               "MXN": 25.2879, "THB": 36.799, "ZAR": 18.1025, "HKD": 9.4010}
CITY_INFO = {  # city: (country, currency)
    "Paris": ("France", "EUR"), "Rome": ("Italy", "EUR"), "New York": ("United States", "USD"),
    "Sydney": ("Australia", "AUD"), "Rio de Janeiro": ("Brazil", "BRL"), "Istanbul": ("Turkey", "TRY"),
    "Mexico City": ("Mexico", "MXN"), "Bangkok": ("Thailand", "THB"), "Cape Town": ("South Africa", "ZAR"),
    "Hong Kong": ("Hong Kong", "HKD"),
}
PRICE_CAP_PERCENTILE = 0.99   # above this (per city) a price is treated as an outlier
PRICE_MIN_USD = 5             # below this a price is not believable
MIN_NIGHTS_CAP = 365          # 9999 is just a placeholder in the raw data
BEDROOMS_CAP = 10

# keep a copy of everything I print so it can be saved as cleaning_report.txt
report = []


def say(msg=""):
    print(msg)
    report.append(str(msg))


# ---------------------------------------------------------------- 1. load
def find_file(folder, name, max_depth=2):
    """look for a file by name (any capitalisation), up to 2 folders deep"""
    folder = folder.rstrip(os.sep)
    for root, dirs, files in os.walk(folder):
        if root[len(folder):].count(os.sep) >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d != os.path.basename(OUT_DIR)]
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return None


listings_path = find_file(INPUT_DIR, "Listings.csv")
reviews_path = find_file(INPUT_DIR, "Reviews.csv")
if not listings_path or not reviews_path:
    raise FileNotFoundError(f"Listings.csv / Reviews.csv not found under {INPUT_DIR}")
say(f"Listings: {listings_path}\nReviews:  {reviews_path}")


def read_dates(series, label):
    """the date format differs between files, so try a few and keep the one that reads everything"""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        parsed = pd.to_datetime(series, format=fmt, errors="coerce")
        if parsed.isna().sum() == series.isna().sum():
            say(f"{label}: dates read as {fmt}")
            return parsed
    raise ValueError(f"could not read the dates in {label}")


# Listings.csv has a few bad bytes that are not valid utf-8, so they get replaced here and removed later
L = pd.read_csv(listings_path, encoding="utf-8", encoding_errors="replace", low_memory=False)
R = pd.read_csv(reviews_path, usecols=["listing_id", "review_id", "date"])
say(f"Raw listings: {len(L):,} rows x {L.shape[1]} cols | raw reviews: {len(R):,} rows")
assert L["listing_id"].is_unique, "listing_id should be unique"

# ---------------------------------------------------------------- 2. reviews
R["review_date"] = read_dates(R["date"], "reviews.date")
n_raw_reviews = len(R)

# a few review_ids show up twice (same review linked to two listings on the same day).
# it is one review, so I keep the first copy
n_dup_reviews = int(R["review_id"].duplicated().sum())
R = R.drop_duplicates("review_id", keep="first")
say(f"Reviews: removed {n_dup_reviews} duplicate review_id rows -> {len(R):,} kept")

orphans = int((~R["listing_id"].isin(L["listing_id"])).sum())
say(f"Reviews whose listing is missing from Listings: {orphans}")
R = R[R["listing_id"].isin(L["listing_id"])]

assert R["listing_id"].max() < 2**31
fact_reviews = (R[["listing_id", "review_date"]]
                .astype({"listing_id": "int32"})
                .sort_values(["review_date", "listing_id"])
                .reset_index(drop=True))
say(f"Review dates: {fact_reviews.review_date.min().date()} to {fact_reviews.review_date.max().date()}")

# per-listing review summary, joined into dim_listing later
rev_sum = fact_reviews.groupby("listing_id")["review_date"].agg(
    total_reviews="size", first_review_date="min", last_review_date="max")

# ---------------------------------------------------------------- 3. listings
def clean_text(s):
    s = s.astype("string").str.replace("�", "", regex=False)
    return s.str.replace(r"[\r\n\t]+", " ", regex=True).str.strip()


# names like "PÃ¨re" are utf-8 that was read as latin-1. try to undo that.
BROKEN = re.compile(r"[ÃÂÅâ][^ \t\r\nA-Za-z0-9]")


def fix_name(text):
    if not isinstance(text, str) or not BROKEN.search(text):
        return text
    fixed = text
    for _ in range(3):
        try:
            fixed = fixed.encode("cp1252").decode("utf-8")
        except UnicodeError:
            try:
                fixed = fixed.encode("latin-1").decode("utf-8")
            except UnicodeError:
                break
        if not BROKEN.search(fixed):
            break
    if BROKEN.search(fixed):  # could not repair it, so drop the non-ascii characters
        fixed = re.sub(r"[^\x00-\x7F]+", "", fixed)
    return fixed


for col in ["name", "host_location", "neighbourhood", "property_type", "room_type", "city", "host_response_time"]:
    L[col] = clean_text(L[col])

unexpected = set(L["city"].dropna().unique()) - set(CITY_INFO)
assert not unexpected, f"unexpected cities: {unexpected}"

L["listing_name"] = L["name"].map(fix_name)

# --- price
usd_per_local = {c: ECB_PER_EUR["USD"] / ECB_PER_EUR[cur] for c, (_, cur) in CITY_INFO.items()}
L["price_local"] = L["price"]
L["price_usd_raw"] = L["price_local"] * L["city"].map(usd_per_local)

city_cap_local = L[L["price_local"] > 0].groupby("city")["price_local"].quantile(PRICE_CAP_PERCENTILE)
L["price_cap_local"] = L["city"].map(city_cap_local)

# nothing is deleted, bad prices are only flagged
L["price_status"] = np.select(
    [L["price_local"] <= 0, L["price_usd_raw"] < PRICE_MIN_USD, L["price_local"] > L["price_cap_local"]],
    ["Zero price", f"Below ${PRICE_MIN_USD}", f"Above city {int(PRICE_CAP_PERCENTILE * 100)}th percentile"],
    default="Valid")
L["price_valid"] = (L["price_status"] == "Valid").astype("int8")
L["price_usd"] = L["price_usd_raw"].where(L["price_local"] > 0).round(2)
L["price_usd_clean"] = L["price_usd_raw"].where(L["price_valid"] == 1).round(2)
L["price_per_guest_usd"] = (L["price_usd_clean"] / L["accommodates"].replace(0, np.nan)).round(2)

# price tier = quartile inside each city, so cities compare fairly without worrying about FX
pct_in_city = L.loc[L["price_valid"] == 1].groupby("city")["price_local"].rank(pct=True)
tier_names = ["Q1 Budget", "Q2 Mid-range", "Q3 Upper", "Q4 Premium"]
tier = pd.cut(pct_in_city, [0, .25, .5, .75, 1.0], labels=tier_names, include_lowest=True).astype("string")
L["price_tier"] = tier.reindex(L.index).fillna("No valid price")

# --- numbers that need fixing
L["accommodates"] = L["accommodates"].where(L["accommodates"] > 0)      # 0 guests makes no sense
L["min_nights"] = L["minimum_nights"].clip(upper=MIN_NIGHTS_CAP)
L["bedrooms"] = L["bedrooms"].clip(upper=BEDROOMS_CAP)
n_min_nights_capped = int((L["minimum_nights"] > MIN_NIGHTS_CAP).sum())

beds = L["bedrooms"]
L["bedroom_group"] = np.select([beds.isna(), beds <= 1, beds == 2, beds == 3],
                               ["Not stated", "1", "2", "3"], default="4+")
nights = L["min_nights"]
L["min_nights_group"] = np.select([nights <= 2, nights <= 6, nights <= 27],
                                  ["1-2 nights", "3-6 nights", "7-27 nights"],
                                  default="28+ nights (monthly)")


# --- property type: 144 values is too many for a chart, so group them
def property_group(p):
    p = str(p).lower()
    if re.search(r"hotel|hostel|bed and breakfast|resort|lodge|kezhan", p):
        return "Hotel / B&B / Hostel"
    if re.search(r"apartment|condo|loft|flat|serviced|studio|penthouse|entire floor", p):
        return "Apartment / Condo / Loft"
    if re.search(r"house|townhouse|villa|bungalow|cottage|cabin|chalet|guest suite|guesthouse|casa particular|farm|castle", p):
        return "House / Villa / Cottage"
    return "Other / Unique"


group_of = {p: property_group(p) for p in L["property_type"].dropna().unique()}
L["property_group"] = L["property_type"].map(group_of).fillna("Other / Unique")

# --- ratings
rating = L["review_scores_rating"]
L["has_rating"] = rating.notna().astype("int8")
rating_bands = ["Not rated", "Below 80", "80-89", "90-94", "95-99", "100"]
L["rating_band"] = np.select([rating.isna(), rating < 80, rating < 90, rating < 95, rating < 100],
                             rating_bands[:5], default="100")

# --- t/f columns to 1/0
yes_no = {"t": 1, "f": 0}


def equals(series, value):
    """True where the series equals value, blanks count as False"""
    return (series == value).fillna(False).to_numpy(dtype=bool)


L["instant_bookable_flag"] = L["instant_bookable"].map(yes_no).astype("Int8")
L["instant_bookable_label"] = np.where(equals(L["instant_bookable_flag"], 1), "Instant book", "Request to book")

# --- amenities (stored as a json list). I checked these patterns against the 3,446 distinct names in the file
AMENITY_PATTERNS = {
    "has_wifi": r"^(free )?wifi(\s*[–-].*)?$",
    "has_kitchen": r"^(full |chef's )?kitchen(ette)?$",
    "has_washer": r"^(free |paid )?washer(\s*[–-].*)?$",
    "has_dryer": r"^(free |paid )?dryer(\s*[–-].*)?$",
    "has_heating": r"^(central |radiant )?heating$",
    "has_air_conditioning": r"^((central |portable )?air conditioning|window ac unit)$",
    "has_elevator": r"^elevator$",
    "has_workspace": r"^(dedicated|laptop-friendly) workspace",
    "has_tv": r"^(\d+\" )?(cable |satellite |hd)?tv\b",
    "has_free_parking": r"^free .*parking",
    "has_pool": r"^((shared|private) )?((outdoor|indoor|rooftop|heated|lap|saltwater|infinity) )*pool$",
    "has_gym": r"^((shared|private) )?gym( in building)?$",
    "has_hot_tub": r"^((private|shared) )?hot tub$",
    "has_breakfast": r"^(breakfast|complimentary .*breakfast.*)$",
    "has_self_checkin": r"^(self check-in|lockbox|keypad|smart lock)$",
    "has_balcony": r"^(patio or balcony|balcony|juliet balcony|patio)$",
}
patterns = {k: re.compile(v) for k, v in AMENITY_PATTERNS.items()}

amenity_lists, all_names = [], set()
for raw in L["amenities"]:
    try:
        names = [str(x).lower().replace("\xa0", " ").strip() for x in json.loads(raw)]
    except Exception:
        names = []
    amenity_lists.append(names)
    all_names.update(names)

# match each distinct name once instead of once per listing (much faster)
flags_for_name = {n: [k for k, rx in patterns.items() if rx.match(n)] for n in all_names}
flags_per_listing = [{f for n in names for f in flags_for_name[n]} for names in amenity_lists]
for k in AMENITY_PATTERNS:
    L[k] = np.fromiter((k in fl for fl in flags_per_listing), dtype="int8", count=len(flags_per_listing))

L["amenity_count"] = np.fromiter((len(set(n)) for n in amenity_lists), dtype="int32", count=len(amenity_lists))
n_amen = L["amenity_count"]
L["amenity_band"] = np.select([n_amen <= 10, n_amen <= 20, n_amen <= 30],
                              ["Basic (0-10)", "Standard (11-20)", "Comfortable (21-30)"], default="Rich (31+)")

# --- is the host local or abroad? based on the free-text host_location
COUNTRY_WORDS = {
    "France": {"france", "fr"}, "Italy": {"italy", "italia", "it"}, "United States": {"united states", "us", "usa"},
    "Australia": {"australia", "au"}, "Brazil": {"brazil", "brasil", "br"}, "Turkey": {"turkey", "turkiye", "tr"},
    "Mexico": {"mexico", "mx"}, "Thailand": {"thailand", "th"}, "South Africa": {"south africa", "za"},
    "Hong Kong": {"hong kong", "hk"},
}
CITY_WORDS = {
    "Paris": ["paris"], "Rome": ["rome", "roma"], "Sydney": ["sydney"], "Rio de Janeiro": ["rio de janeiro"],
    "Istanbul": ["istanbul"], "Bangkok": ["bangkok", "krung thep"], "Cape Town": ["cape town"],
    "Hong Kong": ["hong kong"], "Mexico City": ["mexico city", "ciudad de mexico", "cdmx", "distrito federal"],
    "New York": ["new york", "brooklyn", "manhattan", "queens", "bronx", "staten island"],
}


def host_base(location, city):
    if not isinstance(location, str) or not location.strip():
        return "Unknown"
    text = location.lower()
    last_part = text.split(",")[-1].strip()
    if last_part in COUNTRY_WORDS[CITY_INFO[city][0]] or any(w in text for w in CITY_WORDS[city]):
        return "Same country"
    return "Abroad"


L["host_base"] = [host_base(loc, c) for loc, c in zip(L["host_location"], L["city"])]

# --- host fields
L["host_since_date"] = read_dates(L["host_since"], "listings.host_since")
L["superhost_flag"] = L["host_is_superhost"].map(yes_no).astype("Int8")
L["identity_flag"] = L["host_identity_verified"].map(yes_no).astype("Int8")
L["host_listing_count"] = L.groupby("host_id")["listing_id"].transform("size")   # only counts listings in these files
L["host_city_count"] = L.groupby("host_id")["city"].transform("nunique")
L["response_time_clean"] = L["host_response_time"].fillna("Not reported")

n_unknown_host = int(L["host_since_date"].isna().sum())
say(f"Listings with no host details (host_since, superhost, verified all blank): {n_unknown_host}")

# ---------------------------------------------------------------- 4. build the tables
def add_sort_col(df, col, order):
    """adds <col>_order so Power BI can use 'Sort by column'"""
    df[col + "_order"] = df[col].map({v: i + 1 for i, v in enumerate(order)}).astype("Int64")
    assert df[col + "_order"].notna().all(), f"unmapped values in {col}: {set(df[col]) - set(order)}"


def whole(s):
    return s.round().astype("Int64")


# dim_listing
dl = L.merge(rev_sum, left_on="listing_id", right_index=True, how="left")
dl["total_reviews"] = dl["total_reviews"].fillna(0).astype("int32")
dl["has_reviews"] = (dl["total_reviews"] > 0).astype("int8")
dl["is_entire_place"] = (dl["room_type"] == "Entire place").astype("int8")

dim_listing = pd.DataFrame({
    "listing_id": dl["listing_id"], "listing_name": dl["listing_name"], "host_id": dl["host_id"],
    "city": dl["city"], "neighbourhood": dl["neighbourhood"], "latitude": dl["latitude"], "longitude": dl["longitude"],
    "property_type": dl["property_type"], "property_group": dl["property_group"], "room_type": dl["room_type"],
    "is_entire_place": dl["is_entire_place"], "accommodates": whole(dl["accommodates"]),
    "bedrooms": whole(dl["bedrooms"]), "bedroom_group": dl["bedroom_group"],
    "min_nights": dl["min_nights"].astype("int32"), "min_nights_group": dl["min_nights_group"],
    "price_local": dl["price_local"], "price_usd": dl["price_usd"], "price_status": dl["price_status"],
    "price_valid": dl["price_valid"], "price_usd_clean": dl["price_usd_clean"],
    "price_per_guest_usd": dl["price_per_guest_usd"], "price_tier": dl["price_tier"],
    "has_rating": dl["has_rating"], "review_scores_rating": whole(dl["review_scores_rating"]),
    "rating_band": dl["rating_band"],
    "review_scores_accuracy": whole(dl["review_scores_accuracy"]),
    "review_scores_cleanliness": whole(dl["review_scores_cleanliness"]),
    "review_scores_checkin": whole(dl["review_scores_checkin"]),
    "review_scores_communication": whole(dl["review_scores_communication"]),
    "review_scores_location": whole(dl["review_scores_location"]),
    "review_scores_value": whole(dl["review_scores_value"]),
    "instant_bookable": dl["instant_bookable_flag"], "instant_bookable_label": dl["instant_bookable_label"],
    "host_base": dl["host_base"], "amenity_count": dl["amenity_count"], "amenity_band": dl["amenity_band"],
    **{k: dl[k] for k in AMENITY_PATTERNS},
    "total_reviews": dl["total_reviews"], "has_reviews": dl["has_reviews"],
    "first_review_date": dl["first_review_date"], "last_review_date": dl["last_review_date"],
})
add_sort_col(dim_listing, "bedroom_group", ["1", "2", "3", "4+", "Not stated"])
add_sort_col(dim_listing, "min_nights_group", ["1-2 nights", "3-6 nights", "7-27 nights", "28+ nights (monthly)"])
add_sort_col(dim_listing, "price_tier", tier_names + ["No valid price"])
add_sort_col(dim_listing, "rating_band", rating_bands)
add_sort_col(dim_listing, "amenity_band", ["Basic (0-10)", "Standard (11-20)", "Comfortable (21-30)", "Rich (31+)"])
for col in ["neighbourhood", "city", "listing_name", "property_type"]:
    dim_listing[col] = dim_listing[col].astype("string")

# dim_host: one row per host, taking the values from the host's first listing
H = (L.sort_values("listing_id").groupby("host_id", as_index=False)
       .agg(host_since=("host_since_date", "first"), superhost=("superhost_flag", "first"),
            identity_verified=("identity_flag", "first"), response_time=("response_time_clean", "first"),
            response_rate=("host_response_rate", "first"), acceptance_rate=("host_acceptance_rate", "first"),
            host_listing_count=("host_listing_count", "first"), host_city_count=("host_city_count", "first")))
H["host_since_year"] = H["host_since"].dt.year.astype("Int64")
H["host_tenure_years"] = ((SNAPSHOT_DATE - H["host_since"]).dt.days / 365.25).round(1)
H["superhost_label"] = np.select([equals(H["superhost"], 1), equals(H["superhost"], 0)],
                                 ["Superhost", "Regular host"], default="Unknown")
H["identity_label"] = np.select([equals(H["identity_verified"], 1), equals(H["identity_verified"], 0)],
                                ["Verified", "Not verified"], default="Unknown")
n_listings = H["host_listing_count"]
H["host_type"] = np.select([n_listings == 1, n_listings <= 9],
                           ["Single-listing host", "Small multi-listing host (2-9)"],
                           default="Professional host (10+)")
H["response_rate"] = H["response_rate"].round(2)
H["acceptance_rate"] = H["acceptance_rate"].round(2)
dim_host = H[["host_id", "host_since", "host_since_year", "host_tenure_years", "superhost", "superhost_label",
              "identity_verified", "identity_label", "response_time", "response_rate", "acceptance_rate",
              "host_listing_count", "host_type", "host_city_count"]].copy()
add_sort_col(dim_host, "host_type", ["Single-listing host", "Small multi-listing host (2-9)", "Professional host (10+)"])
add_sort_col(dim_host, "response_time", ["within an hour", "within a few hours", "within a day",
                                         "a few days or more", "Not reported"])

# dim_city
dim_city = pd.DataFrame([
    {"city": c, "country": CITY_INFO[c][0], "currency": CITY_INFO[c][1],
     "usd_per_local_unit": round(usd_per_local[c], 6), "fx_date": FX_DATE,
     "price_cap_local": round(float(city_cap_local[c]), 2),
     "price_cap_usd": round(float(city_cap_local[c]) * usd_per_local[c], 2)}
    for c in CITY_INFO])
dim_city["listings"] = dim_city["city"].map(dim_listing["city"].value_counts()).astype("int32")
last_review_by_city = (fact_reviews.merge(dim_listing[["listing_id", "city"]], on="listing_id")
                       .groupby("city")["review_date"].max())
dim_city["last_review_date"] = dim_city["city"].map(last_review_by_city)
dim_city = dim_city.sort_values("listings", ascending=False).reset_index(drop=True)

# dim_date: one row per day, covers every review date
dim_date = pd.DataFrame({"date": pd.date_range("2008-01-01", "2021-03-31", freq="D")})
d = dim_date["date"]
dim_date["year"] = d.dt.year
dim_date["quarter"] = "Q" + d.dt.quarter.astype(str)
dim_date["month_num"] = d.dt.month
dim_date["month_name"] = d.dt.strftime("%b")
dim_date["year_month"] = d.dt.strftime("%Y-%m")
dim_date["month_start"] = d.dt.to_period("M").dt.start_time
dim_date["week_of_year"] = d.dt.isocalendar().week.astype(int)
dim_date["day_of_week_num"] = d.dt.dayofweek + 1           # Monday = 1 ... Sunday = 7
dim_date["day_name"] = d.dt.day_name()
dim_date["is_weekend"] = (dim_date["day_of_week_num"] >= 6).astype(int)
dim_date["covid_period"] = np.where(d < COVID_START, "Pre-COVID (to Feb 2020)", "COVID period (Mar 2020 on)")
dim_date["data_status"] = np.select([d <= LAST_FULL_MONTH_END, d <= SNAPSHOT_DATE],
                                    ["Complete", "Partial (scrape cut-off)"], default="No data")

# neighbourhood_summary: ~660 points for the map instead of 280K listings
neighbourhood_summary = (dim_listing.groupby(["city", "neighbourhood"], observed=True)
                         .agg(listings=("listing_id", "size"), latitude=("latitude", "mean"),
                              longitude=("longitude", "mean"), median_price_usd=("price_usd_clean", "median"),
                              avg_rating=("review_scores_rating", "mean"),
                              pct_entire_place=("is_entire_place", "mean"), total_reviews=("total_reviews", "sum"))
                         .reset_index())
neighbourhood_summary["latitude"] = neighbourhood_summary["latitude"].round(5)
neighbourhood_summary["longitude"] = neighbourhood_summary["longitude"].round(5)
neighbourhood_summary["median_price_usd"] = neighbourhood_summary["median_price_usd"].round(2)
neighbourhood_summary["avg_rating"] = neighbourhood_summary["avg_rating"].round(1)
neighbourhood_summary["pct_entire_place"] = neighbourhood_summary["pct_entire_place"].round(3)

TABLES = {"fact_reviews": fact_reviews, "dim_listing": dim_listing, "dim_host": dim_host, "dim_city": dim_city,
          "dim_date": dim_date, "neighbourhood_summary": neighbourhood_summary}
for name, df in TABLES.items():
    say(f"{name}: {len(df):,} rows x {df.shape[1]} columns")

# ---------------------------------------------------------------- 5. checks
# if any of these fail, a relationship in Power BI would break
assert dim_listing["listing_id"].is_unique and dim_listing["listing_id"].notna().all()
assert dim_host["host_id"].is_unique and dim_host["host_id"].notna().all()
assert dim_city["city"].is_unique
assert fact_reviews.notna().all().all()
assert fact_reviews["listing_id"].isin(dim_listing["listing_id"]).all(), "fact -> dim_listing broken"
assert dim_listing["host_id"].isin(dim_host["host_id"]).all(), "dim_listing -> dim_host broken"
assert dim_listing["city"].isin(dim_city["city"]).all(), "dim_listing -> dim_city broken"
assert fact_reviews["review_date"].between(dim_date["date"].min(), dim_date["date"].max()).all(), "dates outside dim_date"
assert dim_date["date"].is_unique and dim_date["date"].is_monotonic_increasing
assert (dim_listing["price_usd_clean"].dropna() > 0).all()
assert dim_listing["price_valid"].eq(dim_listing["price_usd_clean"].notna().astype(int)).all()
assert len(fact_reviews) == n_raw_reviews - n_dup_reviews - orphans
assert dim_listing["total_reviews"].sum() == len(fact_reviews)
for col in list(AMENITY_PATTERNS) + ["has_rating", "price_valid", "is_entire_place", "has_reviews"]:
    assert set(dim_listing[col].unique()) <= {0, 1}, col
say("\nAll validation checks passed")

# ---------------------------------------------------------------- 6. cleaning report
say("\n================ CLEANING REPORT ================")
say(f"Listings in: {len(L):,} | listings out: {len(dim_listing):,} (none removed; problem values are flagged, not deleted)")
say(f"Reviews in: {n_raw_reviews:,} | duplicate review_id removed: {n_dup_reviews} | reviews out: {len(fact_reviews):,}")
say("\nPrice status (price_usd_clean is filled only for 'Valid'):")
say(dim_listing["price_status"].value_counts().to_string())
say("\nPrice cap per city (99th percentile of local price):")
say(dim_city[["city", "currency", "price_cap_local", "price_cap_usd"]].to_string(index=False))
say(f"\nUSD per local unit (ECB, {FX_DATE}):")
say(dim_city[["city", "currency", "usd_per_local_unit"]].to_string(index=False))
say(f"\nmin_nights capped at {MIN_NIGHTS_CAP}: {n_min_nights_capped} listings | bedrooms capped at {BEDROOMS_CAP}: "
    f"{int((L['bedrooms'] >= BEDROOMS_CAP).sum())} listings at the cap | accommodates=0 set to blank: "
    f"{int((L['accommodates'].isna()).sum())}")
say("Dropped columns: maximum_nights (junk values), district (87% blank), host_total_listings_count (unreliable: "
    "0 for 33K rows), host_has_profile_pic (99.6% 'true'), host_location (replaced by host_base), reviewer_id, review_id")
say(f"\nListing name repaired for mojibake. Listings with a rating: {int(dim_listing.has_rating.sum()):,} "
    f"({dim_listing.has_rating.mean():.1%}); listings with at least one review: {int(dim_listing.has_reviews.sum()):,}")
say("\nAmenity flag coverage (% of listings):")
say((dim_listing[list(AMENITY_PATTERNS)].mean() * 100).round(1).to_string())
say("\nHost base (vs listing city):")
say(dim_listing["host_base"].value_counts().to_string())
say("\nHost type (hosts):")
say(dim_host["host_type"].value_counts().to_string())
say("\nMedian valid nightly price in USD by city:")
say(dim_listing.groupby("city")["price_usd_clean"].median().round(1).sort_values(ascending=False).to_string())
say("\nReviews per year:")
say(fact_reviews.groupby(fact_reviews["review_date"].dt.year).size().to_string())
say(f"\nUse reviews up to {LAST_FULL_MONTH_END.date()} for trends: later months are partial because each city was "
    f"scraped on a different day:")
say(dim_city[["city", "last_review_date"]].sort_values("last_review_date").to_string(index=False))

# ---------------------------------------------------------------- 7. data dictionary
DICT = {
 "fact_reviews": {
  "listing_id": "Listing that was reviewed (links to dim_listing)",
  "review_date": "Date the review was written (links to dim_date). One row = one review"},
 "dim_listing": {
  "listing_id": "Unique listing ID (primary key)", "listing_name": "Listing title (text repaired where possible)",
  "host_id": "Host ID (links to dim_host)", "city": "City (links to dim_city)", "neighbourhood": "Neighbourhood name",
  "latitude": "Latitude", "longitude": "Longitude", "property_type": "Original property type (144 values)",
  "property_group": "Property type grouped into 4 groups", "room_type": "Entire place / Private room / Hotel room / Shared room",
  "is_entire_place": "1 if room_type is Entire place", "accommodates": "Max guests (0 set to blank)",
  "bedrooms": "Bedrooms (capped at 10; blank if not stated)", "bedroom_group": "1 / 2 / 3 / 4+ / Not stated",
  "min_nights": "Minimum nights per booking (capped at 365)", "min_nights_group": "Short vs monthly-rental style listings",
  "price_local": "Nightly price in the city's local currency (as in the source)",
  "price_usd": "Nightly price in USD at ECB rate of 26 Feb 2021 (blank if price is 0)",
  "price_status": "Valid / Zero price / Below $5 / Above city 99th percentile",
  "price_valid": "1 if price_status is Valid", "price_usd_clean": "USD price for valid prices only. USE THIS for price analysis",
  "price_per_guest_usd": "price_usd_clean divided by accommodates", "price_tier": "Quartile of price inside the city (Q1 Budget ... Q4 Premium)",
  "has_rating": "1 if the listing has a rating", "review_scores_rating": "Overall rating, 20-100 (blank = not rated)",
  "rating_band": "Not rated / Below 80 / 80-89 / 90-94 / 95-99 / 100",
  "review_scores_accuracy": "Accuracy score, 2-10", "review_scores_cleanliness": "Cleanliness score, 2-10",
  "review_scores_checkin": "Check-in score, 2-10", "review_scores_communication": "Communication score, 2-10",
  "review_scores_location": "Location score, 2-10", "review_scores_value": "Value score, 2-10",
  "instant_bookable": "1 if instant book", "instant_bookable_label": "Instant book / Request to book",
  "host_base": "Same country / Abroad / Unknown, from the host's stated location versus the listing's country. 'Abroad' is the reliable bucket",
  "amenity_count": "Number of amenities listed", "amenity_band": "Basic / Standard / Comfortable / Rich",
  **{k: "1 if the listing has this amenity" for k in AMENITY_PATTERNS},
  "total_reviews": "Reviews for this listing in the reviews file", "has_reviews": "1 if total_reviews > 0",
  "first_review_date": "Date of first review", "last_review_date": "Date of latest review",
  "bedroom_group_order": "Sort helper", "min_nights_group_order": "Sort helper", "price_tier_order": "Sort helper",
  "rating_band_order": "Sort helper", "amenity_band_order": "Sort helper"},
 "dim_host": {
  "host_id": "Unique host ID (primary key)", "host_since": "Date the host joined", "host_since_year": "Year host joined",
  "host_tenure_years": "Years on Airbnb at 1 Mar 2021", "superhost": "1 = Superhost, 0 = not, blank = unknown",
  "superhost_label": "Superhost / Regular host / Unknown", "identity_verified": "1 = identity verified, 0 = not, blank = unknown",
  "identity_label": "Verified / Not verified / Unknown", "response_time": "Host response time ('Not reported' if blank)",
  "response_rate": "Share of messages answered, 0-1 (blank for ~46% of hosts)", "acceptance_rate": "Share of requests accepted, 0-1",
  "host_listing_count": "Listings this host has IN THESE FILES (use this, not the original host_total_listings_count)",
  "host_type": "Single / Small multi (2-9) / Professional (10+)", "host_city_count": "Number of these 10 cities the host operates in",
  "host_type_order": "Sort helper", "response_time_order": "Sort helper"},
 "dim_city": {
  "city": "City (primary key)", "country": "Country", "currency": "Local currency code",
  "usd_per_local_unit": "USD per 1 unit of local currency (ECB, 26 Feb 2021)", "fx_date": "Date of FX rate",
  "price_cap_local": "99th percentile of local price in this city (outlier cut-off)", "price_cap_usd": "Same cut-off in USD",
  "listings": "Listings in the file", "last_review_date": "Latest review in the file (scrape cut-off proxy)"},
 "dim_date": {
  "date": "Calendar date (primary key; mark as the date table in Power BI)", "year": "Year", "quarter": "Q1-Q4",
  "month_num": "1-12", "month_name": "Jan-Dec (sort by month_num)", "year_month": "YYYY-MM text",
  "month_start": "First day of the month (use for continuous monthly axes)", "week_of_year": "ISO week number",
  "day_of_week_num": "Monday = 1 ... Sunday = 7", "day_name": "Mon-Sun name (sort by day_of_week_num)",
  "is_weekend": "1 for Saturday/Sunday", "covid_period": "Pre-COVID (to Feb 2020) / COVID period (Mar 2020 on)",
  "data_status": "Complete (to 31 Jan 2021) / Partial (scrape cut-off) / No data"},
 "neighbourhood_summary": {
  "city": "City", "neighbourhood": "Neighbourhood", "listings": "Number of listings", "latitude": "Average latitude",
  "longitude": "Average longitude", "median_price_usd": "Median valid USD price", "avg_rating": "Average overall rating",
  "pct_entire_place": "Share of listings that are entire places (0-1)", "total_reviews": "Total reviews"},
}


def simple_type(s):
    if pd.api.types.is_datetime64_any_dtype(s):
        return "date"
    if pd.api.types.is_numeric_dtype(s):
        return "number"
    return "text"


dict_rows = []
for table, df in TABLES.items():
    assert set(DICT[table]) == set(df.columns), f"dictionary mismatch in {table}: {set(DICT[table]) ^ set(df.columns)}"
    for col in df.columns:
        dict_rows.append({"table": table, "column": col, "type": simple_type(df[col]), "description": DICT[table][col]})
data_dictionary = pd.DataFrame(dict_rows)

# ---------------------------------------------------------------- 8. save
os.makedirs(OUT_DIR, exist_ok=True)
for name, df in TABLES.items():
    df.to_csv(os.path.join(OUT_DIR, f"{name}.csv"), index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")
data_dictionary.to_csv(os.path.join(OUT_DIR, "data_dictionary_clean.csv"), index=False, encoding="utf-8-sig")

say(f"\nFiles written to {OUT_DIR}:")
for fn in sorted(os.listdir(OUT_DIR)):
    say(f"  {fn:32s} {os.path.getsize(os.path.join(OUT_DIR, fn)) / 1e6:8.1f} MB")

with open(os.path.join(OUT_DIR, "cleaning_report.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(report))

print(f"\nDone. Power BI files are in: {OUT_DIR}")