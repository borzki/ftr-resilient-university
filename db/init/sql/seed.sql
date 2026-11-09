-- Synthetic data only (no real people). Deterministic via setseed.
SELECT setseed(0.42);

INSERT INTO students (id, first_name, last_name, email, program, year)
SELECT 1000 + n,
       (ARRAY['Aruzhan','Dias','Madina','Nursultan','Aigerim','Timur','Dana','Arman','Zarina','Ilyas',
              'Kamila','Yerlan','Saule','Daniyar','Alina'])[1 + (n % 15)],
       (ARRAY['Test','Sample','Demo','Synthetic','Mock','Example'])[1 + (n % 6)],
       'student' || (1000 + n) || '@uni.test',
       (ARRAY['Computer Science','Software Engineering','Cybersecurity','Data Science'])[1 + (n % 4)],
       1 + (n % 4)
FROM generate_series(1, 60) AS n;

INSERT INTO courses (code, title, credits, day, start_time, room) VALUES
 ('CSE301', 'Fault Tolerance and Dependable Computing', 5, 'Mon', '09:00', 'A-101'),
 ('CSE302', 'Distributed Systems',                       5, 'Tue', '10:00', 'A-204'),
 ('CSE303', 'Computer Networks',                         5, 'Wed', '11:00', 'B-110'),
 ('CSE304', 'Database Systems',                          5, 'Thu', '09:00', 'B-215'),
 ('CSE305', 'Information Security',                      5, 'Fri', '14:00', 'C-301'),
 ('MAT201', 'Probability and Statistics',                4, 'Mon', '14:00', 'D-102'),
 ('CSE306', 'Cloud Computing',                           5, 'Tue', '15:00', 'A-305'),
 ('CSE307', 'Software Testing',                          4, 'Wed', '09:00', 'C-120');

INSERT INTO enrollments (student_id, course_code, term)
SELECT s.id, c.code, 'Fall-2026'
FROM students s
JOIN LATERAL (SELECT code FROM courses ORDER BY md5(code || s.id) LIMIT 4) c ON true;

INSERT INTO grades_seed (student_id, course_code, term, grade)
SELECT s.id, c.code, 'Spring-2026',
       (ARRAY['A','A-','B+','B','B-','C+','C','A','B+','A-'])[1 + floor(random() * 10)::int]
FROM students s
JOIN LATERAL (SELECT code FROM courses ORDER BY md5(s.id || code) LIMIT 4) c ON true;
INSERT INTO grades SELECT * FROM grades_seed;

INSERT INTO student_accounts (student_id, tuition_due)
SELECT id, 5000.00 FROM students;

-- Test accounts: admin, registrar, and one login per student (s1001 ... s1060).
INSERT INTO users (username, password_hash, role, student_id) VALUES
 ('admin',     crypt(:'seed_password', gen_salt('bf', 8)), 'admin', NULL),
 ('registrar', crypt(:'seed_password', gen_salt('bf', 8)), 'registrar', NULL);
INSERT INTO users (username, password_hash, role, student_id)
SELECT 's' || id, crypt(:'seed_password', gen_salt('bf', 6)), 'student', id FROM students;
