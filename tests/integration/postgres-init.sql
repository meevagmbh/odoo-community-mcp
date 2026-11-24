-- Odoo must not connect as a PostgreSQL superuser (enforced by Odoo master / next release).
CREATE ROLE odoo WITH LOGIN PASSWORD 'odoo' NOSUPERUSER CREATEDB;
