-- infra/services/inventory_service/init.sql
-- Runs automatically on inventory-db's first startup (mounted into
-- Postgres's docker-entrypoint-initdb.d) — creates the table
-- inventory-service's /inventory/{item_id} endpoint actually queries.
CREATE TABLE inventory (
    item_id VARCHAR(64) PRIMARY KEY,
    quantity INTEGER NOT NULL
);

INSERT INTO inventory (item_id, quantity) VALUES
    ('sku-001', 42),
    ('sku-002', 7),
    ('sku-003', 0);
