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
  ('admin', 'pbkdf2_sha256$120000$1f9c14bd0be8ca0058e3ff0caeb82f67$4a1117b432fa24a7d0d86fbb47dadb50ae8cb6393c27ccfbb76b535273d7813b', '系統管理員', NULL, 'admin', NULL),
  ('pm01', 'pbkdf2_sha256$120000$39b488a04954ddb68496a29630cddca6$310704b818292ce95c5e5cd7db72b2aa5c6d822da7eef3e806255d72c2cbd46a', '王專案', 'EMP-001', 'manager', (SELECT id FROM sites WHERE code = 'SITE-A')),
  ('safe01', 'pbkdf2_sha256$120000$03e45a1dbc31f910102dc13f502ed245$779d0ae5ad4adbf08c700c8e22f821c21843a42723ecdda8ef8ae516009dab39', '陳職安', 'EMP-007', 'safety', (SELECT id FROM sites WHERE code = 'SITE-A')),
  ('eng01', 'pbkdf2_sha256$120000$cf457c1b654ae8a4d0ea08b248a648b3$8f345dc21c4f9e8614d64523567052569b0c89f9c7f4fb7d50f9643a30d6b495', '林工程師', 'EMP-012', 'engineer', (SELECT id FROM sites WHERE code = 'SITE-A')),
  ('insp01', 'pbkdf2_sha256$120000$5e02dd5642130b2b1754a244f96f17cd$0fb218c111526625184c6e728b58051eec3114ba8e5f8d1b9c47cd7f573b9782', '張小明', 'EMP-081', 'inspector', (SELECT id FROM sites WHERE code = 'SITE-C')),
  ('insp02', 'pbkdf2_sha256$120000$8daeca041af04f1f1147e2cba5702749$202bd9ed2bbaebbedd9dc2a83104f96c0477335e6d94f6199380088717e47cd8', '李小華', 'EMP-137', 'inspector', (SELECT id FROM sites WHERE code = 'SITE-C')),
  ('insp03', 'pbkdf2_sha256$120000$0a49d4bde48183c1c27374527f939cae$c84ec1b14eea6a0b9c0d8814690343b2469f1e5bc3a4528a061818f31c0ddd96', '吳大明', 'EMP-055', 'inspector', (SELECT id FROM sites WHERE code = 'SITE-B'))
ON CONFLICT (username) DO UPDATE SET
  display_name = EXCLUDED.display_name, role = EXCLUDED.role,
  employee_no = EXCLUDED.employee_no, site_id = EXCLUDED.site_id;
