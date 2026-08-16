"""
JPJ Car Registration Data Warehouse - ETL
------------------------------------------
Source: data.gov.my "Car Registration Transactions" (CC BY 4.0)
        https://data.gov.my/data-catalogue/registration_transactions_car
Target: MySQL star schema for Power BI

Usage:
    pip install pandas pyarrow sqlalchemy pymysql requests

    python etl_load.py --dry-run          # inspect only, don't touch the DB
    python etl_load.py                    # full run
    python etl_load.py --years 2023 2026  # smaller date range

fact_registration is aggregated - one row per date/model/state/colour/fuel,
not one row per registration (see AGGREGATE below).
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

# ---- config: edit these ----

MYSQL = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "password",   # change before running for real
    "database": "carmart_dw",
}

START_YEAR = 2019
END_YEAR = 2026          # current year is partial, portal updates it as the year goes
AGGREGATE = True          # False = raw transaction grain, way more rows
CACHE_DIR = Path("./data_cache")
BASE_URL = "https://storage.data.gov.my/transportation/cars_{year}.{ext}"

# ---- extract ----

EXPECTED_COLS = ["date_reg", "type", "maker", "model", "colour", "fuel", "state"]


# make sure the column names match what the rest of the script expects
def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Portal has used both 'date_reg' and 'date' as the column name over the years."""
    df.columns = [c.strip().lower() for c in df.columns]
    if "date" in df.columns and "date_reg" not in df.columns:
        df = df.rename(columns={"date": "date_reg"})

    # stop here with a clear error instead of a confusing KeyError later on
    required = ["date_reg", "maker", "model", "state"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        sys.exit(
            f"\nERROR: source file is missing required column(s): {missing}\n"
            f"Columns actually present: {list(df.columns)}\n"
            f"Portal probably renamed a column - update `required` above."
        )

    # colour/fuel/type aren't always in the source - fill with 'Unknown' so the schema still builds
    for c in ["colour", "fuel", "type"]:
        if c not in df.columns:
            print(f"  ! note: '{c}' not in source, filling with 'Unknown'")
            df[c] = "Unknown"

    return df


# go grab one year's file from data.gov.my
def fetch_year(year: int) -> pd.DataFrame | None:
    """
    Download one year's file into a DataFrame.

    Using requests + BytesIO instead of pd.read_parquet(url) directly -
    that needs fsspec/aiohttp and fails in a confusing way if they're missing.

    Tries parquet first, falls back to csv.
    """
    import io
    import requests

    for ext, reader in (("parquet", pd.read_parquet), ("csv", pd.read_csv)):
        url = BASE_URL.format(year=year, ext=ext)
        try:
            resp = requests.get(url, timeout=120)
        except requests.exceptions.RequestException as exc:
            print(f"  {year}: network error contacting the server: {exc}")
            return None

        if resp.status_code == 404:
            print(f"  {year}: no .{ext} file published (404)")
            continue
        if not resp.ok:
            print(f"  {year}: server returned HTTP {resp.status_code}")
            continue

        try:
            return reader(io.BytesIO(resp.content))
        except Exception as exc:
            print(f"  {year}: downloaded but couldn't parse .{ext}: {exc}")
            continue

    return None


# loop through every year - use the cached file if I already have it, otherwise download it - then combine into one table
def extract(years) -> pd.DataFrame:
    CACHE_DIR.mkdir(exist_ok=True)
    frames = []

    for year in years:
        cache_file = CACHE_DIR / f"cars_{year}.parquet"

        if cache_file.exists():
            print(f"  {year}: reading from cache")
            df = pd.read_parquet(cache_file)
        else:
            print(f"  {year}: downloading...")
            df = fetch_year(year)
            if df is None:
                print(f"  {year}: skipped")
                continue
            df.to_parquet(cache_file, index=False)  # only cache once we know the parse actually worked

        df = normalise_columns(df)
        print(f"  {year}: {len(df):,} rows")
        frames.append(df)

    if not frames:
        sys.exit("No data extracted. Check your network connection.")

    return pd.concat(frames, ignore_index=True)


# ---- transform ----

# fix up messy dates and text (title case, extra spaces, inconsistent spelling) before anything else touches the data
def clean(df: pd.DataFrame) -> pd.DataFrame:
    print("\nCleaning...")
    before = len(df)

    df["date_reg"] = pd.to_datetime(df["date_reg"], errors="coerce")
    df = df.dropna(subset=["date_reg"])

    # maker/model come in upper-case, colour is lower-case - just title-case everything
    for col in ["maker", "model", "colour", "fuel", "state", "type"]:
        if col in df.columns:
            df[col] = (
                df[col].astype("string").fillna("Unknown").str.strip()
                .str.replace("_", " ", regex=False)  # "hybrid_petrol" -> "hybrid petrol"
                .str.replace("-", " ", regex=False)  # "Rolls-Royce" vs "Rolls Royce" was counting as 2 different makers
                .str.replace(r"\s+", " ", regex=True)
                .str.title()
            )

    df = df[df["date_reg"].dt.year.between(START_YEAR, END_YEAR)]

    print(f"  {before:,} -> {len(df):,} rows after cleaning")
    return df


# build the calendar lookup table - every single date, not just the ones with data
def build_dim_date(df: pd.DataFrame) -> pd.DataFrame:
    """Need every calendar date here (not just ones with registrations) for DAX time intelligence to work right."""
    lo, hi = df["date_reg"].min(), df["date_reg"].max()
    dates = pd.date_range(lo, hi, freq="D")

    dim = pd.DataFrame({"full_date": dates})
    dim["date_key"] = dim["full_date"].dt.strftime("%Y%m%d").astype(int)
    dim["year"] = dim["full_date"].dt.year
    dim["quarter"] = dim["full_date"].dt.quarter
    dim["quarter_name"] = "Q" + dim["quarter"].astype(str)
    dim["month_number"] = dim["full_date"].dt.month
    dim["month_name"] = dim["full_date"].dt.strftime("%b")
    dim["month_year"] = dim["full_date"].dt.strftime("%b %Y")
    dim["day_of_month"] = dim["full_date"].dt.day
    dim["day_name"] = dim["full_date"].dt.strftime("%a")
    dim["is_weekend"] = dim["full_date"].dt.dayofweek.isin([5, 6])
    dim["month_year_sort"] = dim["full_date"].dt.strftime("%Y%m").astype(int)  # so "Jan 2024" sorts before "Feb 2024" instead of alphabetically

    return dim[
        [
            "date_key", "full_date", "year", "quarter", "quarter_name",
            "month_number", "month_name", "month_year", "month_year_sort",
            "day_of_month", "day_name", "is_weekend",
        ]
    ]


# build a small lookup table (models, states, etc) with a simple ID number for each unique value
def build_dim(df: pd.DataFrame, cols, key_name: str) -> pd.DataFrame:
    """Unique combos of the given columns + a surrogate key. Used for all the small dims."""
    dim = df[cols].drop_duplicates().sort_values(cols).reset_index(drop=True)
    dim.insert(0, key_name, range(1, len(dim) + 1))
    return dim


# take the cleaned data and rebuild it into the star schema - the small lookup tables plus the big counts table
def transform(df: pd.DataFrame):
    print("\nBuilding dimensions...")

    dim_date = build_dim_date(df)
    dim_model = build_dim(df, ["maker", "model"], "model_key")
    dim_state = build_dim(df, ["state"], "state_key")
    dim_colour = build_dim(df, ["colour"], "colour_key")
    dim_fuel = build_dim(df, ["fuel"], "fuel_key")

    for name, d in [
        ("dim_date", dim_date), ("dim_model", dim_model),
        ("dim_state", dim_state), ("dim_colour", dim_colour),
        ("dim_fuel", dim_fuel),
    ]:
        print(f"  {name}: {len(d):,} rows")

    print("\nBuilding fact table...")
    fact = df.merge(dim_model, on=["maker", "model"], how="left")
    fact = fact.merge(dim_state, on="state", how="left")
    fact = fact.merge(dim_colour, on="colour", how="left")
    fact = fact.merge(dim_fuel, on="fuel", how="left")
    fact["date_key"] = fact["date_reg"].dt.strftime("%Y%m%d").astype(int)

    keys = ["date_key", "model_key", "state_key", "colour_key", "fuel_key"]

    # check before the groupby, not after - it silently drops NaN-key rows,
    # so checking after would hide the problem instead of catching it
    orphans = fact[keys].isna().sum()
    if orphans.sum():
        print("\nERROR: rows failed to join to a dimension:")
        for col, count in orphans[orphans > 0].items():
            print(f"    {col}: {count:,} rows")
        sys.exit("Aborting load - fact table would be incomplete.")
    print("  referential integrity: OK")

    source_rows = len(fact)

    if AGGREGATE:
        fact = (
            fact.groupby(keys, as_index=False)
            .size()
            .rename(columns={"size": "registration_count"})
        )
    else:
        fact = fact[keys].copy()
        fact["registration_count"] = 1

    # registration_count summed up should match the row count pre-aggregation - proves nothing got lost or double counted
    total = int(fact["registration_count"].sum())
    if total != source_rows:
        sys.exit(f"ERROR: reconciliation failed. Source had {source_rows:,} "
                 f"rows but fact totals {total:,}.")

    fact.insert(0, "registration_key", range(1, len(fact) + 1))
    ratio = len(fact) / source_rows * 100
    print(f"  fact_registration: {len(fact):,} rows "
          f"({total:,} registrations, {ratio:.0f}% of source grain)")
    print(f"  reconciliation: OK ({total:,} = {source_rows:,})")

    return {
        "dim_date": dim_date,
        "dim_model": dim_model,
        "dim_state": dim_state,
        "dim_colour": dim_colour,
        "dim_fuel": dim_fuel,
        "fact_registration": fact,
    }


# ---- load ----

# connect to MySQL, write all the tables in, then set up indexes so it's fast to query
def load(tables: dict):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import URL

    # URL.create() escapes special chars (@, :, /, # etc.) in the password - an f-string here would break on those
    url = URL.create(
        "mysql+pymysql",
        username=MYSQL["user"],
        password=MYSQL["password"],
        host=MYSQL["host"],
        port=MYSQL["port"],
        database=MYSQL["database"],
    )
    try:
        engine = create_engine(url)
        with engine.connect():
            pass
    except Exception as exc:
        sys.exit(
            f"\nERROR: could not connect to MySQL.\n"
            f"  host={MYSQL['host']} port={MYSQL['port']} "
            f"user={MYSQL['user']} database={MYSQL['database']}\n"
            f"  {type(exc).__name__}: {exc}\n\n"
            f"Check MySQL80 is running (services.msc) and that the "
            f"password in the MYSQL block above matches MySQL Workbench."
        )

    print("\nLoading to MySQL...")
    order = ["dim_date", "dim_model", "dim_state", "dim_colour",  # dims before the fact table, just good star-schema practice
             "dim_fuel", "fact_registration"]

    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))

    for name in order:
        df = tables[name]
        # chunksize kept small - method="multi" batches rows into one INSERT and a huge batch can hit max_allowed_packet
        df.to_sql(name, engine, if_exists="replace", index=False,
                  chunksize=5_000, method="multi")
        print(f"  {name}: {len(df):,} rows loaded")

    print("\nCreating indexes...")  # without these, Power BI refresh queries against the fact table get painfully slow
    idx = [
        "ALTER TABLE dim_date ADD PRIMARY KEY (date_key)",
        "ALTER TABLE dim_model ADD PRIMARY KEY (model_key)",
        "ALTER TABLE dim_state ADD PRIMARY KEY (state_key)",
        "ALTER TABLE dim_colour ADD PRIMARY KEY (colour_key)",
        "ALTER TABLE dim_fuel ADD PRIMARY KEY (fuel_key)",
        "ALTER TABLE fact_registration ADD PRIMARY KEY (registration_key)",
        "CREATE INDEX ix_fact_date ON fact_registration (date_key)",
        "CREATE INDEX ix_fact_model ON fact_registration (model_key)",
        "CREATE INDEX ix_fact_state ON fact_registration (state_key)",
    ]
    with engine.begin() as conn:
        for stmt in idx:
            try:
                conn.execute(text(stmt))
            except Exception as exc:
                print(f"  skipped: {stmt.split(' ON ')[0][:50]} ({type(exc).__name__})")
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    print("  done")


# runs the whole pipeline in order - extract, clean, transform, load (or just a dry run if that flag is passed)
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="extract and transform only, skip DB load")
    ap.add_argument("--years", nargs=2, type=int, metavar=("START", "END"))
    args = ap.parse_args()

    global START_YEAR, END_YEAR
    if args.years:
        START_YEAR, END_YEAR = args.years

    print(f"JPJ Car Registration ETL | {START_YEAR}-{END_YEAR}")
    print("=" * 55)

    raw = extract(range(START_YEAR, END_YEAR + 1))
    clean_df = clean(raw)
    tables = transform(clean_df)

    if args.dry_run:
        print("\n--dry-run: skipping load. Sample of fact table:\n")
        print(tables["fact_registration"].head(10).to_string(index=False))
        print("\nTop makers:")
        top = (clean_df.groupby("maker").size()
               .sort_values(ascending=False).head(10))
        print(top.to_string())
    else:
        load(tables)
        print("\nDone. Connect Power BI to MySQL "
              f"database '{MYSQL['database']}'.")


if __name__ == "__main__":
    main()
