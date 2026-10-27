-- Synthetic DEMO banking schema (all invented): creates schema demo_banking only. Safe next to a real "banking" schema.
DROP SCHEMA IF EXISTS demo_banking CASCADE;
CREATE SCHEMA demo_banking;
CREATE TABLE demo_banking.branches (branch_id int PRIMARY KEY, branch_name text NOT NULL, region text NOT NULL);
COMMENT ON TABLE demo_banking.branches IS 'Bank branches.';
CREATE TABLE demo_banking.customers (
  customer_id int PRIMARY KEY, full_name text NOT NULL, email text, phone text, address text, city text,
  date_of_birth date, ssn text, branch_id int REFERENCES demo_banking.branches, segment text, created_at timestamptz);
CREATE TABLE demo_banking.accounts (
  account_id int PRIMARY KEY, customer_id int REFERENCES demo_banking.customers, account_number text NOT NULL,
  account_type text NOT NULL, status text NOT NULL, opened_at date NOT NULL, balance numeric(14,2) NOT NULL, currency text NOT NULL);
COMMENT ON COLUMN demo_banking.accounts.balance IS 'Current balance in the account currency.';
CREATE TABLE demo_banking.transactions (
  txn_id bigint PRIMARY KEY, account_id int REFERENCES demo_banking.accounts, txn_date date NOT NULL,
  amount numeric(12,2) NOT NULL, txn_type text NOT NULL, channel text NOT NULL, merchant_category text);
COMMENT ON COLUMN demo_banking.transactions.amount IS 'Positive = money in, negative = money out.';
CREATE TABLE demo_banking.loans (
  loan_id int PRIMARY KEY, customer_id int REFERENCES demo_banking.customers, loan_type text NOT NULL,
  principal numeric(14,2) NOT NULL, interest_rate numeric(5,2) NOT NULL, status text NOT NULL, issued_at date NOT NULL);

INSERT INTO demo_banking.branches VALUES
 (1,'Seattle Downtown','West'),(2,'Portland Pearl','West'),(3,'Boston Back Bay','East'),(4,'NYC Midtown','East'),
 (5,'Chicago Loop','North'),(6,'Dallas Uptown','South');
INSERT INTO demo_banking.customers
SELECT g, (array['Ava','Liam','Mia','Noah','Zoe','Ethan','Ivy','Omar'])[1+g%8] || ' ' || (array['Reyes','Khan','Novak','Ito','Mensah','Silva'])[1+g%6],
       'cust'||g||'@example.com', '+1-206-555-01'||lpad((g%100)::text,2,'0'), (100+g)||' Pine St',
       (array['Seattle','Portland','Boston','New York','Chicago','Dallas'])[1+(g%6)],
       date '1960-01-01' + (g*37 % 15000), lpad((g*7919 % 1000000000)::text, 9, '0'), 1+(g%6),
       (array['Retail','Premier','Business'])[1+g%3], timestamptz '2022-01-01' + (g || ' days')::interval
FROM generate_series(1,600) g;
INSERT INTO demo_banking.accounts
SELECT g, 1+(g%600), 'GB29NWBK6016133192'||lpad(g::text,4,'0'), (array['checking','savings','credit_card'])[1+g%3],
       (array['active','active','active','dormant','closed'])[1+g%5], date '2023-01-01' + (g % 900), round((g*137.31 % 25000)::numeric,2), 'USD'
FROM generate_series(1,1200) g;
INSERT INTO demo_banking.transactions
SELECT g, 1+(g%1200), date '2026-01-01' + (g % 300), round(((g*53.7 % 900) - 450)::numeric, 2),
       (array['card','transfer','deposit','withdrawal','fee'])[1+g%5], (array['mobile','branch','online','atm'])[1+g%4],
       (array['groceries','travel','dining','utilities',NULL])[1+g%5]
FROM generate_series(1,30000) g;
INSERT INTO demo_banking.loans
SELECT g, 1+(g*7%600), (array['mortgage','auto','personal'])[1+g%3], round((5000 + g*911 % 400000)::numeric,2),
       round((3 + (g%70)/10.0)::numeric,2), (array['current','current','late','paid_off'])[1+g%4], date '2021-01-01' + (g*3 % 1900)
FROM generate_series(1,400) g;
-- Like a Supabase dashboard table: RLS on, a policy only for its own API roles.
ALTER TABLE demo_banking.customers ENABLE ROW LEVEL SECURITY;
ANALYZE demo_banking.branches, demo_banking.customers, demo_banking.accounts, demo_banking.transactions, demo_banking.loans;
