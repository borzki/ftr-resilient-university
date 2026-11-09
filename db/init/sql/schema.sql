-- University Information System schema
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE students (
    id          INT PRIMARY KEY,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    program     TEXT NOT NULL,
    year        INT  NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,             -- bcrypt via pgcrypto crypt()
    role          TEXT NOT NULL CHECK (role IN ('admin', 'registrar', 'student')),
    student_id    INT REFERENCES students(id)
);

CREATE TABLE courses (
    code        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    credits     INT  NOT NULL,
    day         TEXT NOT NULL,
    start_time  TIME NOT NULL,
    room        TEXT NOT NULL
);

CREATE TABLE enrollments (
    student_id  INT  NOT NULL REFERENCES students(id),
    course_code TEXT NOT NULL REFERENCES courses(code),
    term        TEXT NOT NULL,
    PRIMARY KEY (student_id, course_code, term)
);

-- Business validation lives in the application layer; the baseline configuration
-- does not validate, which is what the misuse experiments measure.
CREATE TABLE grades (
    student_id  INT  NOT NULL REFERENCES students(id),
    course_code TEXT NOT NULL,
    term        TEXT NOT NULL,
    grade       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (student_id, course_code, term)
);

CREATE TABLE grades_seed (LIKE grades INCLUDING ALL);

CREATE TABLE student_accounts (
    student_id  INT PRIMARY KEY REFERENCES students(id),
    tuition_due NUMERIC(12,2) NOT NULL,
    total_paid  NUMERIC(12,2) NOT NULL DEFAULT 0
);

CREATE TABLE payments (
    id              BIGSERIAL PRIMARY KEY,
    student_id      INT NOT NULL REFERENCES students(id),
    amount          NUMERIC(14,2) NOT NULL,
    term            TEXT NOT NULL,
    method          TEXT NOT NULL,
    client_ref      TEXT,                    -- client's request reference (both modes, for analysis)
    idempotency_key TEXT UNIQUE,             -- enforced only by the resilient configuration
    request_id      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX payments_student_idx ON payments(student_id);
CREATE INDEX payments_client_ref_idx ON payments(client_ref);

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    service     TEXT NOT NULL,
    actor       TEXT NOT NULL,
    role        TEXT NOT NULL,
    action      TEXT NOT NULL,
    resource    TEXT,
    outcome     TEXT NOT NULL,
    request_id  TEXT,
    details     JSONB
);

-- Restores the known initial state between experiments.
CREATE FUNCTION reset_test_state() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    TRUNCATE payments RESTART IDENTITY;
    TRUNCATE audit_log RESTART IDENTITY;
    UPDATE student_accounts SET total_paid = 0;
    DELETE FROM grades;
    INSERT INTO grades SELECT * FROM grades_seed;
    DELETE FROM users WHERE student_id > 1060;
    DELETE FROM enrollments WHERE student_id > 1060;
    DELETE FROM student_accounts WHERE student_id > 1060;
    DELETE FROM students WHERE id > 1060;
    UPDATE students SET status = 'active';
END $$;

-- Integrity checks used by the experiments.
CREATE VIEW integrity_report AS
SELECT
  (SELECT count(*) FROM payments)                                            AS payment_rows,
  (SELECT count(*) FROM (SELECT client_ref FROM payments WHERE client_ref IS NOT NULL
                         GROUP BY client_ref HAVING count(*) > 1) d)           AS duplicated_refs,
  (SELECT coalesce(sum(c - 1), 0) FROM (SELECT count(*) c FROM payments WHERE client_ref IS NOT NULL
                         GROUP BY client_ref) d)                               AS duplicate_rows,
  (SELECT count(*) FROM student_accounts a
     WHERE a.total_paid <> coalesce((SELECT sum(amount) FROM payments p
                                     WHERE p.student_id = a.student_id), 0))  AS balance_mismatches,
  (SELECT count(*) FROM payments WHERE amount <= 0 OR amount > 100000
       OR term !~ '^(Fall|Spring|Summer)-20[2-3][0-9]$'
       OR method NOT IN ('card','bank_transfer','cash'))                      AS invalid_payments,
  (SELECT count(*) FROM grades WHERE grade NOT IN
       ('A','A-','B+','B','B-','C+','C','C-','D+','D','F'))                   AS invalid_grades,
  (SELECT md5(string_agg(student_id || course_code || term || grade, ',' ORDER BY student_id, course_code, term))
     FROM grades)                                                              AS grades_checksum;
