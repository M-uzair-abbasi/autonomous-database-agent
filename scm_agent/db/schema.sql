-- Sample e-commerce schema for local development and evaluation.
--
-- Deliberately shaped to exercise the query patterns our error analysis cares
-- about: 4-table joins (customers -> orders -> order_items -> products),
-- aggregates, date filtering, and nested subqueries.
--
-- All data below is synthetic. No real people, no production data.
-- Dates are ISO 8601 (YYYY-MM-DD).

DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    customer_id  SERIAL PRIMARY KEY,
    full_name    VARCHAR(120)  NOT NULL,
    email        VARCHAR(160)  NOT NULL UNIQUE,
    city         VARCHAR(80),
    country      VARCHAR(80),
    signup_date  DATE          NOT NULL
);

CREATE TABLE products (
    product_id   SERIAL PRIMARY KEY,
    name         VARCHAR(160)  NOT NULL,
    category     VARCHAR(80)   NOT NULL,
    unit_price   NUMERIC(10,2) NOT NULL CHECK (unit_price >= 0),
    in_stock     INTEGER       NOT NULL DEFAULT 0 CHECK (in_stock >= 0)
);

CREATE TABLE orders (
    order_id     SERIAL PRIMARY KEY,
    customer_id  INTEGER       NOT NULL REFERENCES customers(customer_id),
    order_date   DATE          NOT NULL,
    status       VARCHAR(20)   NOT NULL DEFAULT 'pending'
);

CREATE TABLE order_items (
    order_item_id SERIAL PRIMARY KEY,
    order_id      INTEGER       NOT NULL REFERENCES orders(order_id),
    product_id    INTEGER       NOT NULL REFERENCES products(product_id),
    quantity      INTEGER       NOT NULL CHECK (quantity > 0),
    unit_price    NUMERIC(10,2) NOT NULL CHECK (unit_price >= 0)
);

CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_date     ON orders(order_date);
CREATE INDEX idx_items_order     ON order_items(order_id);
CREATE INDEX idx_items_product   ON order_items(product_id);


INSERT INTO customers (full_name, email, city, country, signup_date) VALUES
    ('Ayesha Karim',   'ayesha.karim@example.com',   'Lahore',    'Pakistan',      '2024-01-15'),
    ('Bilal Ahmed',    'bilal.ahmed@example.com',    'Karachi',   'Pakistan',      '2024-02-03'),
    ('Chen Wei',       'chen.wei@example.com',       'Singapore', 'Singapore',     '2024-02-20'),
    ('Daniyal Danish', 'daniyal.danish@example.com', 'Topi',      'Pakistan',      '2024-03-11'),
    ('Elena Rossi',    'elena.rossi@example.com',    'Milan',     'Italy',         '2024-04-02'),
    ('Farah Nadeem',   'farah.nadeem@example.com',   'Islamabad', 'Pakistan',      '2024-05-19'),
    ('George Miller',  'george.miller@example.com',  'Leeds',     'United Kingdom','2024-06-07'),
    ('Hina Sohail',    'hina.sohail@example.com',    'Peshawar',  'Pakistan',      '2024-07-25'),
    -- Deliberately has NO orders. Without a customer like this, a LEFT JOIN and
    -- an INNER JOIN return identical results, and the evaluation set cannot
    -- detect a model that confuses the two.
    ('Imran Shah',     'imran.shah@example.com',     'Quetta',    'Pakistan',      '2024-11-02');

INSERT INTO products (name, category, unit_price, in_stock) VALUES
    ('Mechanical Keyboard',   'Peripherals',  89.99,  42),
    ('27in 4K Monitor',       'Displays',    329.50,  15),
    ('Wireless Mouse',        'Peripherals',  24.75, 130),
    ('USB-C Docking Station', 'Accessories', 149.00,  23),
    ('Noise Cancel Headset',  'Audio',       199.99,  31),
    ('1TB NVMe SSD',          'Storage',     109.90,  58),
    ('Laptop Stand',          'Accessories',  39.25,  76),
    ('HD Webcam',             'Peripherals',  64.40,   0),
    ('Ergonomic Chair',       'Furniture',   279.00,   7),
    ('Desk Lamp',             'Furniture',    32.10,  94),
    -- Deliberately never appears in order_items, for the same LEFT JOIN reason
    -- as Imran Shah above: it makes "products that have never sold" answerable.
    ('Cable Organiser',       'Accessories',  12.50, 210);

INSERT INTO orders (customer_id, order_date, status) VALUES
    (1, '2024-03-01', 'delivered'),
    (1, '2024-06-14', 'delivered'),
    (2, '2024-03-22', 'cancelled'),
    (2, '2024-08-05', 'shipped'),
    (3, '2024-04-10', 'delivered'),
    (4, '2024-05-02', 'delivered'),
    (4, '2024-09-18', 'pending'),
    (5, '2024-06-30', 'delivered'),
    (6, '2024-07-12', 'shipped'),
    (7, '2024-08-21', 'delivered'),
    (8, '2024-09-03', 'pending'),
    (8, '2024-10-11', 'delivered');

INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES
    ( 1,  1, 1,  89.99),
    ( 1,  3, 2,  24.75),
    ( 2,  2, 1, 329.50),
    ( 2,  7, 1,  39.25),
    ( 3,  5, 1, 199.99),
    ( 4,  6, 2, 109.90),
    ( 4,  3, 1,  24.75),
    ( 4, 10, 3,  32.10),
    ( 5,  9, 1, 279.00),
    ( 5,  4, 1, 149.00),
    ( 6,  1, 2,  89.99),
    ( 6,  8, 1,  64.40),
    ( 7,  2, 2, 329.50),
    ( 8,  5, 1, 199.99),
    ( 8,  7, 2,  39.25),
    ( 9,  6, 1, 109.90),
    ( 9, 10, 1,  32.10),
    (10,  4, 2, 149.00),
    (10,  1, 1,  89.99),
    (10,  3, 4,  24.75),
    (11,  9, 1, 279.00),
    (11,  2, 1, 329.50),
    (12,  5, 2, 199.99),
    (12,  6, 1, 109.90),
    (12,  8, 1,  64.40);
