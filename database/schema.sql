-- =============================================================================
-- Melbourne Real Estate Intelligence & Property Valuation Platform
-- PostgreSQL + PostGIS Spatial Schema
-- =============================================================================

-- Enable PostGIS spatial extension
CREATE EXTENSION IF NOT EXISTS postgis;

-- -----------------------------------------------------------------------------
-- 1. Suburbs Table
-- Stores Victorian suburb boundaries (GDA94 / WGS84 EPSG:4326 MultiPolygon)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suburbs (
    id SERIAL PRIMARY KEY,
    suburb_name VARCHAR(120) NOT NULL,
    postcode VARCHAR(10) NOT NULL,
    region VARCHAR(120) NOT NULL,
    lga_name VARCHAR(150),
    median_house_price NUMERIC(14, 2),
    boundary_geom GEOMETRY(MultiPolygon, 4326),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_suburb_postcode UNIQUE (suburb_name, postcode)
);

CREATE INDEX IF NOT EXISTS idx_suburbs_geom ON suburbs USING GIST (boundary_geom);
CREATE INDEX IF NOT EXISTS idx_suburbs_name ON suburbs (suburb_name);
CREATE INDEX IF NOT EXISTS idx_suburbs_postcode ON suburbs (postcode);

-- -----------------------------------------------------------------------------
-- 2. Properties Table
-- Master cadastre-level property records with Point geometry (EPSG:4326)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS properties (
    id SERIAL PRIMARY KEY,
    normalized_address VARCHAR(255) NOT NULL UNIQUE,
    street_address VARCHAR(255) NOT NULL,
    suburb_id INTEGER NOT NULL REFERENCES suburbs(id) ON DELETE RESTRICT,
    land_size_sqm NUMERIC(10, 2) CHECK (land_size_sqm >= 0),
    building_area_sqm NUMERIC(10, 2) CHECK (building_area_sqm >= 0),
    bedrooms SMALLINT NOT NULL DEFAULT 0 CHECK (bedrooms >= 0),
    bathrooms SMALLINT NOT NULL DEFAULT 0 CHECK (bathrooms >= 0),
    car_spaces SMALLINT NOT NULL DEFAULT 0 CHECK (car_spaces >= 0),
    property_type VARCHAR(50) NOT NULL CHECK (
        property_type IN ('House', 'Townhouse', 'Apartment', 'Unit', 'Vacant Land', 'Other')
    ),
    latitude DOUBLE PRECISION NOT NULL CHECK (latitude BETWEEN -39.5 AND -33.5),
    longitude DOUBLE PRECISION NOT NULL CHECK (longitude BETWEEN 140.5 AND 150.5),
    location_geom GEOMETRY(Point, 4326) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_properties_location_geom ON properties USING GIST (location_geom);
CREATE INDEX IF NOT EXISTS idx_properties_suburb_id ON properties (suburb_id);
CREATE INDEX IF NOT EXISTS idx_properties_type_beds ON properties (property_type, bedrooms);

-- Trigger function to automatically synchronize location_geom from longitude/latitude
CREATE OR REPLACE FUNCTION sync_property_location_geom()
RETURNS TRIGGER AS $$
BEGIN
    NEW.location_geom := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_property_geom ON properties;
CREATE TRIGGER trg_sync_property_geom
BEFORE INSERT OR UPDATE OF latitude, longitude ON properties
FOR EACH ROW
EXECUTE FUNCTION sync_property_location_geom();

-- -----------------------------------------------------------------------------
-- 3. Listings Table
-- Live and historical market campaigns with agent price guides and AVM outputs
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listings (
    id SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    external_listing_id VARCHAR(100),
    source VARCHAR(50) NOT NULL DEFAULT 'Domain/REA',
    first_listed_date DATE NOT NULL,
    status VARCHAR(40) NOT NULL DEFAULT 'Active' CHECK (
        status IN ('Active', 'Under Offer', 'Sold', 'Withdrawn', 'Archived')
    ),
    price_guide_raw VARCHAR(150),
    price_guide_low NUMERIC(14, 2) CHECK (price_guide_low > 0),
    price_guide_high NUMERIC(14, 2) CHECK (price_guide_high >= price_guide_low),
    agent_name VARCHAR(120),
    agency_name VARCHAR(150),
    avm_estimated_value NUMERIC(14, 2),
    underquoting_gap_pct NUMERIC(6, 2),
    underquoting_risk_level VARCHAR(30),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_listings_property_id ON listings (property_id);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings (status);
CREATE INDEX IF NOT EXISTS idx_listings_price_range ON listings (price_guide_low, price_guide_high);

-- -----------------------------------------------------------------------------
-- 4. Listing Price Revisions Table
-- Timestamped audit log of price adjustments to identify distressed vendors
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listing_price_revisions (
    id SERIAL PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    previous_price_low NUMERIC(14, 2),
    previous_price_high NUMERIC(14, 2),
    new_price_low NUMERIC(14, 2) NOT NULL,
    new_price_high NUMERIC(14, 2) NOT NULL,
    price_change_pct NUMERIC(6, 2),
    revision_reason VARCHAR(120) DEFAULT 'Vendor Price Adjustment',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_revisions_listing_id ON listing_price_revisions (listing_id);
CREATE INDEX IF NOT EXISTS idx_price_revisions_recorded_at ON listing_price_revisions (recorded_at DESC);

-- Trigger to automatically log price revisions when a listing's price guide changes
CREATE OR REPLACE FUNCTION log_listing_price_revision()
RETURNS TRIGGER AS $$
DECLARE
    old_mid NUMERIC(14, 2);
    new_mid NUMERIC(14, 2);
    pct_diff NUMERIC(6, 2);
BEGIN
    IF (OLD.price_guide_low IS DISTINCT FROM NEW.price_guide_low)
       OR (OLD.price_guide_high IS DISTINCT FROM NEW.price_guide_high) THEN

        old_mid := COALESCE((OLD.price_guide_low + OLD.price_guide_high) / 2.0, OLD.price_guide_low, 0);
        new_mid := COALESCE((NEW.price_guide_low + NEW.price_guide_high) / 2.0, NEW.price_guide_low, 0);

        IF old_mid > 0 THEN
            pct_diff := ROUND(((new_mid - old_mid) / old_mid) * 100.0, 2);
        ELSE
            pct_diff := NULL;
        END IF;

        INSERT INTO listing_price_revisions (
            listing_id,
            previous_price_low,
            previous_price_high,
            new_price_low,
            new_price_high,
            price_change_pct,
            revision_reason
        ) VALUES (
            NEW.id,
            OLD.price_guide_low,
            OLD.price_guide_high,
            NEW.price_guide_low,
            NEW.price_guide_high,
            pct_diff,
            CASE
                WHEN pct_diff < 0 THEN 'Price Reduction / Distressed Vendor Signal'
                WHEN pct_diff > 0 THEN 'Upward Guide Revision'
                ELSE 'Guide Range Restructuring'
            END
        );
    END IF;
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_log_listing_price_revision ON listings;
CREATE TRIGGER trg_log_listing_price_revision
BEFORE UPDATE ON listings
FOR EACH ROW
EXECUTE FUNCTION log_listing_price_revision();

-- -----------------------------------------------------------------------------
-- 5. Settled Sales Table
-- Valuer-General Victoria historical settled sales for AVM training & comps
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settled_sales (
    id SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    contract_date DATE,
    settlement_date DATE NOT NULL,
    sold_price NUMERIC(14, 2) NOT NULL CHECK (sold_price > 0),
    sale_method VARCHAR(50) NOT NULL DEFAULT 'Auction' CHECK (
        sale_method IN ('Auction', 'Private Treaty', 'Expression of Interest', 'Off-Market', 'Valuer-General Record')
    ),
    days_on_market SMALLINT CHECK (days_on_market >= 0),
    source_reference VARCHAR(100) DEFAULT 'Valuer-General Victoria',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_settled_sales_property_id ON settled_sales (property_id);
CREATE INDEX IF NOT EXISTS idx_settled_sales_settlement_date ON settled_sales (settlement_date DESC);
CREATE INDEX IF NOT EXISTS idx_settled_sales_sold_price ON settled_sales (sold_price);

