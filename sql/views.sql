-- views Power BI reads from - keeps the heavier aggregation in SQL so
-- the Power BI model stays light and refreshes fast
-- needs MySQL 8.0+ (uses window functions)

USE carmart_dw;

-- main view Power BI connects to - flattened instead of using the star schema directly, easier to check during UAT
CREATE OR REPLACE VIEW vw_registrations AS
SELECT
    f.registration_key,
    d.full_date,
    d.year,
    d.quarter_name,
    d.month_name,
    d.month_year,
    d.month_year_sort,
    m.maker,
    m.model,
    s.state,
    c.colour,
    fu.fuel,
    f.registration_count
FROM fact_registration f
JOIN dim_date   d  ON f.date_key   = d.date_key
JOIN dim_model  m  ON f.model_key  = m.model_key
JOIN dim_state  s  ON f.state_key  = s.state_key
JOIN dim_colour c  ON f.colour_key = c.colour_key
JOIN dim_fuel   fu ON f.fuel_key   = fu.fuel_key;


-- registrations by maker per month + market share (window function instead of a self-join)
CREATE OR REPLACE VIEW vw_maker_monthly AS
SELECT
    d.year,
    d.month_number,
    d.month_year,
    d.month_year_sort,
    m.maker,
    SUM(f.registration_count) AS registrations,
    ROUND(
        100.0 * SUM(f.registration_count)
        / SUM(SUM(f.registration_count)) OVER (PARTITION BY d.month_year_sort),
        2
    ) AS market_share_pct
FROM fact_registration f
JOIN dim_date  d ON f.date_key  = d.date_key
JOIN dim_model m ON f.model_key = m.model_key
GROUP BY d.year, d.month_number, d.month_year, d.month_year_sort, m.maker;


-- Perodua model numbers year over year - LAG() pulls last year's number onto the same row
CREATE OR REPLACE VIEW vw_perodua_model_yoy AS
WITH yearly AS (
    SELECT
        d.year,
        m.model,
        SUM(f.registration_count) AS registrations
    FROM fact_registration f
    JOIN dim_date  d ON f.date_key  = d.date_key
    JOIN dim_model m ON f.model_key = m.model_key
    WHERE m.maker = 'Perodua'
    GROUP BY d.year, m.model
)
SELECT
    year,
    model,
    registrations,
    LAG(registrations) OVER (PARTITION BY model ORDER BY year) AS prior_year,
    ROUND(
        100.0 * (registrations - LAG(registrations) OVER (PARTITION BY model ORDER BY year))
        / NULLIF(LAG(registrations) OVER (PARTITION BY model ORDER BY year), 0),
        2
    ) AS yoy_growth_pct,
    RANK() OVER (PARTITION BY year ORDER BY registrations DESC) AS rank_in_year
FROM yearly;


-- Perodua's share of each state - CASE WHEN inside SUM so it's one pass over the table instead of two
CREATE OR REPLACE VIEW vw_state_penetration AS
SELECT
    d.year,
    s.state,
    SUM(f.registration_count) AS total_registrations,
    SUM(CASE WHEN m.maker = 'Perodua' THEN f.registration_count ELSE 0 END)
        AS perodua_registrations,
    ROUND(
        100.0 * SUM(CASE WHEN m.maker = 'Perodua' THEN f.registration_count ELSE 0 END)
        / NULLIF(SUM(f.registration_count), 0),
        2
    ) AS perodua_share_pct
FROM fact_registration f
JOIN dim_date  d ON f.date_key  = d.date_key
JOIN dim_model m ON f.model_key = m.model_key
JOIN dim_state s ON f.state_key = s.state_key
GROUP BY d.year, s.state;


-- fuel type mix by year, for the hybrid/EV growth trend
CREATE OR REPLACE VIEW vw_fuel_mix AS
SELECT
    d.year,
    fu.fuel,
    SUM(f.registration_count) AS registrations,
    ROUND(
        100.0 * SUM(f.registration_count)
        / SUM(SUM(f.registration_count)) OVER (PARTITION BY d.year),
        2
    ) AS pct_of_year
FROM fact_registration f
JOIN dim_date d  ON f.date_key = d.date_key
JOIN dim_fuel fu ON f.fuel_key = fu.fuel_key
GROUP BY d.year, fu.fuel;


-- sanity checks to run before UAT sign-off
CREATE OR REPLACE VIEW vw_dq_checks AS
SELECT 'Fact row count'          AS check_name,
       CAST(COUNT(*) AS CHAR)    AS result FROM fact_registration
UNION ALL
SELECT 'Total registrations',
       CAST(SUM(registration_count) AS CHAR) FROM fact_registration
UNION ALL
SELECT 'Orphaned model keys',
       CAST(COUNT(*) AS CHAR)
FROM fact_registration f
LEFT JOIN dim_model m ON f.model_key = m.model_key
WHERE m.model_key IS NULL
UNION ALL
SELECT 'Date range',
       CONCAT(MIN(full_date), ' to ', MAX(full_date)) FROM dim_date
UNION ALL
SELECT 'Distinct makers',
       CAST(COUNT(DISTINCT maker) AS CHAR) FROM dim_model;
