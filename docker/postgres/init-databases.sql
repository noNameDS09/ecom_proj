-- One database per service: services never share a schema (or a Postgres role
-- beyond bootstrap in this demo). Applied by the postgres image on first boot.
CREATE DATABASE product_db;
CREATE DATABASE user_db;
CREATE DATABASE order_db;
CREATE DATABASE payment_db;
