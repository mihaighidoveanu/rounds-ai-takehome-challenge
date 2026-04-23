-- Schema for the AI Engineer take-home analytics dataset.
-- Runs automatically on first boot via /docker-entrypoint-initdb.d.

CREATE TABLE apps (
    app_id   TEXT NOT NULL PRIMARY KEY,
    name     TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('iOS', 'Android'))
);

CREATE TABLE daily_metrics (
    app_id         TEXT           NOT NULL REFERENCES apps(app_id),
    date           DATE           NOT NULL,
    country        CHAR(2)        NOT NULL,
    installs       BIGINT         NOT NULL,
    in_app_revenue NUMERIC(12, 2) NOT NULL,
    ads_revenue    NUMERIC(12, 2) NOT NULL,
    ua_cost        NUMERIC(12, 2) NOT NULL,
    PRIMARY KEY (app_id, date, country)
);

CREATE INDEX daily_metrics_date_idx    ON daily_metrics (date);
CREATE INDEX daily_metrics_country_idx ON daily_metrics (country);
