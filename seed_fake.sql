-- Полная очистка danных, кроме teachers.
PRAGMA foreign_keys = ON;
BEGIN;

DELETE FROM support_claims;
DELETE FROM support_calls;
DELETE FROM responses;
DELETE FROM surveys;
DELETE FROM memberships;
DELETE FROM persons;
DELETE FROM groups;

-- Сбросим автоинкременты, чтобы id были чистенькие.
DELETE FROM sqlite_sequence WHERE name IN ('groups','persons','memberships','surveys','responses','support_calls','support_claims');

-- Anubis (teacher_id=2, tg 6664457053) становится учеником у Godblazer (teacher_id=1).
INSERT INTO persons (teacher_id, tg_user_id, tg_username, full_name, gender, helped_count, support_level)
VALUES (1, 6664457053, 'anubisae', 'Анубис', 'male', 1, 3);

-- Фейковые ученики у Godblazer.
-- gender: male/female, support_level: 0..3 (NULL — не саппорт).
INSERT INTO persons (teacher_id, tg_user_id, tg_username, full_name, gender, helped_count, support_level) VALUES
  (1, 1000001, 'alex_m',        'Алексей Морозов',     'male',   0, NULL),
  (1, 1000002, 'mary_k',        'Мария Кузнецова',     'female', 2, 2),
  (1, 1000003, 'dima_v',        'Дмитрий Васильев',    'male',   5, 3),
  (1, 1000004, 'anna_s',        'Анна Соколова',       'female', 1, 1),
  (1, 1000005, 'kate_p',        'Екатерина Павлова',   'female', 0, NULL),
  (1, 1000006, 'igor_r',        'Игорь Романов',       'male',   3, 2),
  (1, 1000007, 'olya_t',        'Ольга Титова',        'female', 4, 3),
  (1, 1000008, 'sergey_l',      'Сергей Лебедев',      'male',   0, NULL),
  (1, 1000009, 'nastya_b',      'Анастасия Белова',    'female', 2, 2),
  (1, 1000010, 'pavel_z',       'Павел Зайцев',        'male',   1, 1),
  (1, 1000011, 'lena_o',        'Елена Орлова',        'female', 0, NULL),
  (1, 1000012, 'mikhail_n',     'Михаил Никитин',      'male',   6, 3),
  (1, 1000013, 'sveta_g',       'Светлана Громова',    'female', 3, 2),
  (1, 1000014, 'andrey_f',      'Андрей Фёдоров',      'male',   0, NULL),
  (1, 1000015, 'julia_e',       'Юлия Ефремова',       'female', 1, 1),
  (1, 1000016, 'roma_h',        'Роман Харитонов',     'male',   2, 2),
  (1, 1000017, 'tanya_y',       'Татьяна Ярцева',      'female', 0, NULL),
  (1, 1000018, 'kirill_d',      'Кирилл Денисов',      'male',   4, 3);

-- Фейковые группы у Godblazer.
INSERT INTO groups (teacher_id, name, weekdays, lesson_time, lead_hours, join_code, active) VALUES
  (1, 'Бачата начинающие',  '0,2',  '19:00', 18, 'BACH-BEG', 1),
  (1, 'Бачата средний',     '1,3',  '20:00', 18, 'BACH-MID', 1),
  (1, 'Сальса базовый',     '0,4',  '18:30', 18, 'SALS-BAS', 1),
  (1, 'Кизомба',            '5',    '14:00', 24, 'KIZ-WKND', 1),
  (1, 'Сальса продвинутые', '6',    '17:00', 24, 'SALS-ADV', 1),
  (1, 'Хастл (архив)',      '2',    '21:00', 18, 'HUSTL-OLD', 0);

-- Memberships. id: Анубис=1, остальные 2..19. group_id: Bach-Beg=1 .. Salsa-Adv=5, Hustl=6.
-- Анубис ходит в Бачата начинающие + Кизомба.
INSERT INTO memberships (person_id, group_id) VALUES
  (1, 1), (1, 4),
  -- Бачата начинающие (g1): 6 учеников
  (2, 1), (3, 1), (5, 1), (8, 1), (12, 1),
  -- Бачата средний (g2): 6 учеников + Anubis НЕ ходит
  (3, 2), (4, 2), (7, 2), (10, 2), (13, 2), (16, 2),
  -- Сальса базовый (g3): 5
  (5, 3), (6, 3), (9, 3), (11, 3), (14, 3),
  -- Кизомба (g4): 4 ученика + Anubis (уже выше)
  (2, 4), (7, 4), (15, 4), (18, 4),
  -- Сальса продвинутые (g5): 4 ученика, продвинутые саппорты
  (4, 5), (8, 5), (13, 5), (19, 5),
  -- Хастл (g6, неактивная): 3 ученика — для проверки неактивных
  (6, 6), (11, 6), (17, 6);

COMMIT;

-- Контрольный вывод
SELECT 'teachers' AS t, COUNT(*) FROM teachers
UNION ALL SELECT 'groups',      COUNT(*) FROM groups
UNION ALL SELECT 'persons',     COUNT(*) FROM persons
UNION ALL SELECT 'memberships', COUNT(*) FROM memberships;
