-- Least-privilege database roles: each service can only touch its own tables.
CREATE ROLE student_svc   LOGIN PASSWORD :'student_pw';
CREATE ROLE payment_svc   LOGIN PASSWORD :'payment_pw';
CREATE ROLE records_svc   LOGIN PASSWORD :'records_pw';
CREATE ROLE timetable_svc LOGIN PASSWORD :'timetable_pw';
CREATE ROLE replicator    LOGIN REPLICATION PASSWORD :'repl_pw';

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO student_svc, payment_svc, records_svc, timetable_svc;

-- every service may append audit events but not read or change them
GRANT INSERT ON audit_log TO student_svc, payment_svc, records_svc, timetable_svc;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO student_svc, payment_svc, records_svc, timetable_svc;

-- student-service: profiles, logins, audit reading (admin endpoint)
GRANT SELECT, INSERT, UPDATE ON students TO student_svc;
GRANT SELECT ON users TO student_svc;
GRANT SELECT ON audit_log TO student_svc;

-- payment-service: payments and balances only
GRANT SELECT, INSERT ON payments TO payment_svc;
GRANT USAGE ON SEQUENCE payments_id_seq TO payment_svc;
GRANT SELECT, UPDATE ON student_accounts TO payment_svc;

-- records-service: grades (read/write) and the course catalogue (read)
GRANT SELECT, INSERT, UPDATE ON grades TO records_svc;
GRANT SELECT ON courses TO records_svc;

-- timetable-service: read-only
GRANT SELECT ON enrollments, courses TO timetable_svc;
