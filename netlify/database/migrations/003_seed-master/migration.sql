-- 工地、廠商、使用者主檔
-- 由 tools/gen_migrations.py 產生，請勿手動編輯。
-- 密碼雜湊格式與 Python 版 app/auth.py 相同（PBKDF2-SHA256），
-- TypeScript 端以 Web Crypto 驗證，兩邊可互通。

INSERT INTO sites (code, name) VALUES
  ('SITE-A', '示範工地 A'),
  ('SITE-B', '示範工地 B'),
  ('SITE-C', '示範工地 C')
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name;

INSERT INTO vendors (code, name) VALUES
  ('V001', '甲營造'),
  ('V002', '乙機電'),
  ('V003', '丙工程'),
  ('V004', '丁營造'),
  ('V005', '戊工程'),
  ('V006', '己工程')
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name;

INSERT INTO users (username, password_hash, display_name, employee_no, role, site_id) VALUES
  ('admin', 'pbkdf2_sha256$120000$d6b115d095e14cc81cf002fde295ec70$32dfcdbf1cccf489eb48294b0797200eeeea31e70917722623d8f11922c98a97', '系統管理員', NULL, 'admin', NULL),
  ('pm01', 'pbkdf2_sha256$120000$08299b9038b18d3a2afa9d6b13693891$bb50d9a57d74b70c084a99c898ec4c9884e0be2ee95b651dedfebfd53c0d36ec', '王專案', 'EMP-001', 'manager', (SELECT id FROM sites WHERE code = 'SITE-A')),
  ('safe01', 'pbkdf2_sha256$120000$d773de496e83d9e909fc96a2d2f494cd$c68c75b915bc13915b0ae8516659314a1d3e874de71d4017a589a74219de5250', '陳職安', 'EMP-007', 'safety', (SELECT id FROM sites WHERE code = 'SITE-A')),
  ('eng01', 'pbkdf2_sha256$120000$5690325c42440f7ec6936ab18628f2ba$90e160d7fcf39827c7515329b87be12fd5062badffb067b339f5a54fd00dbc72', '林工程師', 'EMP-012', 'engineer', (SELECT id FROM sites WHERE code = 'SITE-A')),
  ('insp01', 'pbkdf2_sha256$120000$e2c4203691b32d881cf32f28355b2147$55a1209a45058b33cae95987824fc8b221f9115915cc704d808dafc4758643fe', '張小明', 'EMP-081', 'inspector', (SELECT id FROM sites WHERE code = 'SITE-C')),
  ('insp02', 'pbkdf2_sha256$120000$c11f01b3f57ca562c87c2e4f69c2b85c$d6b54e97a01312fe53f95ac3d275126e2a7d24f20e3850656c190bb04765c0a7', '李小華', 'EMP-137', 'inspector', (SELECT id FROM sites WHERE code = 'SITE-C')),
  ('insp03', 'pbkdf2_sha256$120000$c57f1256626a8e497aa1a8990242aec0$47416a35d6782f2048d812f587a9ebb63415f1bb2bbc12cf5d89f8ceb89245bd', '吳大明', 'EMP-055', 'inspector', (SELECT id FROM sites WHERE code = 'SITE-B'))
ON CONFLICT (username) DO UPDATE SET
  display_name = EXCLUDED.display_name, role = EXCLUDED.role,
  employee_no = EXCLUDED.employee_no, site_id = EXCLUDED.site_id;
