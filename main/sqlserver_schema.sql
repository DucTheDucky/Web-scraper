-- Run in the selected project database. Python splits this file at GO.
-- Existing application tables are retained; this creates the schema on first use.

IF OBJECT_ID(N'dbo.provinces', N'U') IS NULL
CREATE TABLE dbo.provinces (
    province_id int IDENTITY(1,1) PRIMARY KEY,
    province_name nvarchar(200) NOT NULL UNIQUE
);

IF OBJECT_ID(N'dbo.districts', N'U') IS NULL
CREATE TABLE dbo.districts (
    district_id int IDENTITY(1,1) PRIMARY KEY,
    province_id int NOT NULL REFERENCES dbo.provinces(province_id),
    district_name nvarchar(200) NOT NULL,
    UNIQUE (province_id, district_name)
);

IF OBJECT_ID(N'dbo.wards', N'U') IS NULL
CREATE TABLE dbo.wards (
    ward_id int IDENTITY(1,1) PRIMARY KEY,
    district_id int NOT NULL REFERENCES dbo.districts(district_id),
    ward_name nvarchar(200) NOT NULL,
    UNIQUE (district_id, ward_name)
);

IF OBJECT_ID(N'dbo.legal_statuses', N'U') IS NULL
CREATE TABLE dbo.legal_statuses (
    legal_status_id int IDENTITY(1,1) PRIMARY KEY,
    status_name nvarchar(400) NOT NULL UNIQUE
);

IF OBJECT_ID(N'dbo.directions', N'U') IS NULL
CREATE TABLE dbo.directions (
    direction_id int IDENTITY(1,1) PRIMARY KEY,
    direction_name nvarchar(100) NOT NULL UNIQUE
);

IF OBJECT_ID(N'dbo.contacts', N'U') IS NULL
CREATE TABLE dbo.contacts (
    contact_id int IDENTITY(1,1) PRIMARY KEY,
    contact_name nvarchar(200) NOT NULL,
    seller_type nvarchar(100) NOT NULL DEFAULT N'Unknown',
    UNIQUE (contact_name, seller_type)
);

IF OBJECT_ID(N'dbo.listings', N'U') IS NULL
CREATE TABLE dbo.listings (
    listing_id int IDENTITY(1,1) PRIMARY KEY,
    post_id nvarchar(100) NOT NULL UNIQUE,
    title nvarchar(1000) NOT NULL,
    address_detail nvarchar(2000),
    ward_id int REFERENCES dbo.wards(ward_id),
    contact_id int REFERENCES dbo.contacts(contact_id),
    legal_status_id int REFERENCES dbo.legal_statuses(legal_status_id),
    direction_id int REFERENCES dbo.directions(direction_id),
    price_text nvarchar(200),
    price_vnd bigint,
    price_per_m2 bigint,
    price_period nvarchar(20),
    area decimal(18,4),
    frontage decimal(18,4),
    road_width decimal(18,4),
    is_corner_lot bit,
    verified_status bit,
    post_date date,
    expiration_date date,
    source_url nvarchar(2000) NOT NULL
);

IF OBJECT_ID(N'dbo.listing_audits', N'U') IS NULL
CREATE TABLE dbo.listing_audits (
    listing_id int PRIMARY KEY REFERENCES dbo.listings(listing_id) ON DELETE CASCADE,
    price_per_m2_source nvarchar(30),
    corner_keywords nvarchar(1000),
    seller_keywords nvarchar(1000),
    province_text nvarchar(200),
    district_text nvarchar(200),
    ward_text nvarchar(200),
    seller_type_text nvarchar(100)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID(N'dbo.listings') AND name = N'idx_listings_ward')
CREATE INDEX idx_listings_ward ON dbo.listings(ward_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID(N'dbo.listings') AND name = N'idx_listings_price')
CREATE INDEX idx_listings_price ON dbo.listings(price_vnd);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID(N'dbo.listings') AND name = N'idx_listings_price_m2')
CREATE INDEX idx_listings_price_m2 ON dbo.listings(price_per_m2);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID(N'dbo.listings') AND name = N'idx_listings_area')
CREATE INDEX idx_listings_area ON dbo.listings(area);
GO

-- Add provenance columns without changing existing property records.
IF COL_LENGTH(N'dbo.listing_audits', N'scraped_at') IS NULL
    ALTER TABLE dbo.listing_audits ADD scraped_at datetimeoffset(0) NULL;
IF COL_LENGTH(N'dbo.listing_audits', N'source_list_url') IS NULL
    ALTER TABLE dbo.listing_audits ADD source_list_url nvarchar(2048) NULL;
GO

IF OBJECT_ID(N'dbo.crawl_runs', N'U') IS NULL
CREATE TABLE dbo.crawl_runs (
    run_id uniqueidentifier NOT NULL PRIMARY KEY,
    list_url nvarchar(2048) NOT NULL,
    created_at datetimeoffset(0) NOT NULL,
    last_started_at datetimeoffset(0) NOT NULL,
    last_stop_reason nvarchar(2048) NULL
);

IF OBJECT_ID(N'dbo.crawl_list_pages', N'U') IS NULL
CREATE TABLE dbo.crawl_list_pages (
    seq bigint IDENTITY(1,1) NOT NULL PRIMARY KEY,
    run_id uniqueidentifier NOT NULL REFERENCES dbo.crawl_runs(run_id),
    url nvarchar(2048) NOT NULL,
    url_hash binary(32) NOT NULL,
    status varchar(16) NOT NULL CHECK (status IN ('pending','done')),
    signature char(40) NULL,
    UNIQUE (run_id, url_hash)
);

IF OBJECT_ID(N'dbo.crawl_queue', N'U') IS NULL
CREATE TABLE dbo.crawl_queue (
    seq bigint IDENTITY(1,1) NOT NULL PRIMARY KEY,
    run_id uniqueidentifier NOT NULL REFERENCES dbo.crawl_runs(run_id),
    post_id nvarchar(100) NOT NULL,
    url nvarchar(2048) NOT NULL,
    url_hash binary(32) NOT NULL,
    source_list_url nvarchar(2048) NOT NULL,
    status varchar(16) NOT NULL CHECK (status IN ('pending','done','failed','gone','skipped','duplicate')),
    attempts int NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error nvarchar(max) NULL,
    updated_at datetimeoffset(0) NOT NULL,
    UNIQUE (run_id, post_id),
    UNIQUE (run_id, url_hash)
);

IF OBJECT_ID(N'dbo.crawl_results', N'U') IS NULL
CREATE TABLE dbo.crawl_results (
    run_id uniqueidentifier NOT NULL REFERENCES dbo.crawl_runs(run_id),
    post_id nvarchar(100) NOT NULL,
    listing_id int NOT NULL REFERENCES dbo.listings(listing_id),
    scraped_at datetimeoffset(0) NOT NULL,
    source_list_url nvarchar(2048) NOT NULL,
    data nvarchar(max) NOT NULL CHECK (ISJSON(data) = 1),
    PRIMARY KEY (run_id, post_id),
    FOREIGN KEY (run_id, post_id) REFERENCES dbo.crawl_queue(run_id, post_id)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'dbo.crawl_queue') AND name=N'idx_crawl_queue_pending')
    CREATE INDEX idx_crawl_queue_pending ON dbo.crawl_queue(run_id,status,seq);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'dbo.crawl_list_pages') AND name=N'idx_crawl_pages_pending')
    CREATE INDEX idx_crawl_pages_pending ON dbo.crawl_list_pages(run_id,status,seq);
GO

CREATE OR ALTER VIEW dbo.batdongsan_data
AS
SELECT
    l.title AS Title,
    l.price_text AS Price_Text,
    l.price_vnd AS Price_VND,
    l.price_per_m2 AS Price_Per_M2,
    l.area AS Area,
    l.address_detail AS Address,
    COALESCE(a.province_text, p.province_name) AS Province,
    COALESCE(a.district_text, d.district_name) AS District,
    COALESCE(a.ward_text, w.ward_name) AS Ward,
    l.post_id AS Post_ID,
    l.post_date AS Post_Date,
    l.expiration_date AS Expiration_Date,
    l.verified_status AS Verified_Status,
    s.status_name AS Legal_Status,
    l.road_width AS Road_Width,
    l.frontage AS Frontage,
    r.direction_name AS Direction,
    l.is_corner_lot AS Is_Corner_Lot,
    COALESCE(a.seller_type_text, c.seller_type) AS Seller_Type,
    c.contact_name AS Contact_Name,
    l.source_url AS URL,
    a.price_per_m2_source AS Price_Per_M2_Source,
    l.price_period AS Price_Period,
    a.corner_keywords AS Corner_Keywords,
    a.seller_keywords AS Seller_Keywords,
    a.scraped_at AS Scraped_At,
    a.source_list_url AS Source_List_URL
FROM dbo.listings AS l
LEFT JOIN dbo.listing_audits AS a ON a.listing_id = l.listing_id
LEFT JOIN dbo.wards AS w ON w.ward_id = l.ward_id
LEFT JOIN dbo.districts AS d ON d.district_id = w.district_id
LEFT JOIN dbo.provinces AS p ON p.province_id = d.province_id
LEFT JOIN dbo.contacts AS c ON c.contact_id = l.contact_id
LEFT JOIN dbo.legal_statuses AS s ON s.legal_status_id = l.legal_status_id
LEFT JOIN dbo.directions AS r ON r.direction_id = l.direction_id;
