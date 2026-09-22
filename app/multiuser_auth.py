"""Local server accounts, expiring sessions and append-only operator audit."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
import argparse
import getpass
from pathlib import Path


class AccessDenied(ValueError):
    status = 403


class LoginRequired(AccessDenied):
    status = 401


class Accounts:
    SESSION_SECONDS = 8 * 3600
    REMEMBER_SECONDS = 30 * 24 * 3600

    @classmethod
    def session_seconds(cls, remember=False):
        return cls.REMEMBER_SECONDS if remember is True else cls.SESSION_SECONDS

    ID_PATTERN = re.compile(r'[a-f0-9]{32}')
    DEPARTMENTS = frozenset(('gov', 'food', 'bid'))

    def __init__(self, root, clock=time.time):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path, self.clock = self.root / 'accounts.sqlite3', clock
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL, role TEXT NOT NULL,
                    department TEXT NOT NULL DEFAULT 'gov',
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    csrf_hash TEXT NOT NULL, expires REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id));
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS failures (
                    bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, user_id TEXT,
                    action TEXT NOT NULL, target TEXT NOT NULL, outcome TEXT NOT NULL);
            ''')
            # Databases created before departmental accounts have no column.
            # SQLite's constant default backfills all existing users as gov.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
            if 'department' not in columns:
                db.execute("ALTER TABLE users ADD COLUMN department TEXT NOT NULL DEFAULT 'gov'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()

    @staticmethod
    def public(row):
        return {k: row[k] for k in ('id', 'username', 'display_name', 'role', 'department', 'enabled')}

    @classmethod
    def _department(cls, department):
        if department not in cls.DEPARTMENTS:
            raise ValueError('部门无效')
        return department

    def create(self, username, display_name, password, role='operator', bootstrap=False,
               department='gov'):
        if not isinstance(username, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,40}', username):
            raise ValueError('账号需为3至40位字母、数字、点、横线或下划线')
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 60:
            raise ValueError('请填写1至60字姓名')
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise ValueError('密码需为12至128个字符')
        if role not in ('operator', 'admin'): raise ValueError('角色无效')
        department = self._department(department)
        identity, salt = uuid.uuid4().hex, secrets.token_hex(16)
        hashed = self.password_hash(password, salt)
        try:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if bootstrap and db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
                    raise ValueError('管理员已初始化，请由现有管理员创建账号')
                db.execute('''INSERT INTO users
                    (id,username,display_name,role,department,salt,password_hash,enabled,created)
                    VALUES (?,?,?,?,?,?,?,1,?)''',
                           (identity, username.lower(), display_name.strip(), role, department,
                            salt, hashed, self.clock()))
        except sqlite3.IntegrityError:
            raise ValueError('账号已存在') from None
        return self.user(identity)

    def user(self, identity):
        with self.db() as db:
            row = db.execute('SELECT * FROM users WHERE id=?', (identity,)).fetchone()
        if not row: raise AccessDenied('账号不存在')
        return self.public(row)

    def list(self):
        with self.db() as db:
            return [self.public(row) for row in db.execute('SELECT * FROM users ORDER BY created')]

    def login(self, username, password, peer, remember=False):
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 40 or len(password) > 128:
            raise AccessDenied('账号或密码不正确')
        now = self.clock()
        buckets = ['user:' + username.lower(), 'peer:' + peer]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for bucket in buckets:
                row = db.execute('SELECT * FROM failures WHERE bucket=?', (bucket,)).fetchone()
                if row and row['until'] > now and row['count'] >= (5 if bucket.startswith('user:') else 30):
                    raise AccessDenied('尝试次数过多，请15分钟后再试')
            row = db.execute('SELECT * FROM users WHERE username=?', (username.lower(),)).fetchone()
            salt = row['salt'] if row else '00' * 16
            hashed = self.password_hash(password, salt)
            valid = row and hmac.compare_digest(hashed, row['password_hash']) and row['enabled']
            if not valid:
                for bucket in buckets:
                    db.execute('''INSERT INTO failures VALUES (?,1,?) ON CONFLICT(bucket) DO UPDATE
                        SET count=CASE WHEN until>? THEN count+1 ELSE 1 END, until=?''',
                               (bucket, now+900, now, now+900))
            else:
                token = secrets.token_urlsafe(32)
                csrf = self.csrf(token)
                db.execute('DELETE FROM failures WHERE bucket=?', (buckets[0],))
                db.execute('DELETE FROM sessions WHERE expires<=?', (now,))
                db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                           (hashlib.sha256(token.encode()).hexdigest(), row['id'], hashlib.sha256(csrf.encode()).hexdigest(), now+self.session_seconds(remember)))
        if not valid: raise AccessDenied('账号或密码不正确')
        self.audit(row['id'], 'login', '', 'success')
        return self.public(row), token, csrf

    @staticmethod
    def csrf(token):
        return hashlib.sha256(('csrf:' + token).encode()).hexdigest()

    def authenticate(self, token, csrf=None):
        if not isinstance(token, str) or len(token) > 100: raise LoginRequired('请登录录单工具')
        with self.db() as db:
            row = db.execute('''SELECT users.*, sessions.csrf_hash FROM sessions JOIN users
                ON users.id=sessions.user_id WHERE token_hash=? AND expires>? AND enabled=1''',
                             (hashlib.sha256(token.encode()).hexdigest(), self.clock())).fetchone()
        if not row: raise LoginRequired('登录已过期，请重新登录')
        if csrf is not None and not hmac.compare_digest(hashlib.sha256(csrf.encode()).hexdigest(), row['csrf_hash']):
            raise AccessDenied('登录状态已变化，请重新登录后继续')
        return self.public(row)

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))

    @classmethod
    def _identity(cls, identity):
        if not isinstance(identity, str) or not cls.ID_PATTERN.fullmatch(identity):
            raise ValueError('账号ID无效')
        return identity

    def _admin_actor(self, actor):
        actor = self._identity(actor)
        with self.db() as db:
            return self._admin_actor_row(db, actor)

    def _admin_actor_row(self, db, actor):
        actor = self._identity(actor)
        row = db.execute('SELECT * FROM users WHERE id=?', (actor,)).fetchone()
        if not row or row['role'] != 'admin' or not row['enabled']:
            raise AccessDenied('仅启用的管理员可执行此操作')
        return row

    @staticmethod
    def _password_valid(password):
        return isinstance(password, str) and 12 <= len(password) <= 128

    def _replace_password(self, identity, password, action, actor=None, require_admin=False,
                          admin_only_target=False):
        identity = self._identity(identity)
        if not self._password_valid(password):
            raise ValueError('新密码需为12至128个字符')
        if require_admin:
            self._admin_actor(actor)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM users WHERE id=?', (identity,)).fetchone()
            if not row or (admin_only_target and row['role'] != 'admin'):
                raise AccessDenied('目标账号不存在或不是管理员')
            salt = secrets.token_hex(16)
            db.execute('UPDATE users SET salt=?,password_hash=? WHERE id=?',
                       (salt, self.password_hash(password, salt), identity))
            # A password reset invalidates every existing session for the target.
            db.execute('DELETE FROM sessions WHERE user_id=?', (identity,))
        self.audit(actor, action, identity, 'success')
        return self.public(row)

    def admin_reset_password(self, target, actor, newpassword):
        """Reset an existing account password under an enabled-admin identity."""
        actor = self._identity(actor)
        target = self._identity(target)
        if not self._password_valid(newpassword):
            raise ValueError('新密码需为12至128个字符')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._admin_actor_row(db, actor)
            row = db.execute('SELECT * FROM users WHERE id=?', (target,)).fetchone()
            if not row:
                raise AccessDenied('目标账号不存在')
            salt = secrets.token_hex(16)
            db.execute('UPDATE users SET salt=?,password_hash=? WHERE id=?',
                       (salt, self.password_hash(newpassword, salt), target))
            db.execute('DELETE FROM sessions WHERE user_id=?', (target,))
        self.audit(actor, 'admin_reset_password', target, 'success')
        return self.public(row)

    def enable(self, target, actor):
        """Enable an account under an enabled-admin identity; never creates a session."""
        actor = self._identity(actor)
        target = self._identity(target)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._admin_actor_row(db, actor)
            row = db.execute('SELECT * FROM users WHERE id=?', (target,)).fetchone()
            if not row:
                raise AccessDenied('目标账号不存在')
            db.execute('UPDATE users SET enabled=1 WHERE id=?', (target,))
        self.audit(actor, 'enable_account', target, 'success')
        result = self.public(row)
        result['enabled'] = 1
        return result

    def disable(self, identity, actor):
        actor = self._identity(actor)
        identity = self._identity(identity)
        if identity == actor: raise ValueError('不能停用当前账号')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._admin_actor_row(db, actor)
            row = db.execute('SELECT * FROM users WHERE id=?', (identity,)).fetchone()
            if not row: raise AccessDenied('目标账号不存在')
            if row['role'] == 'admin' and db.execute(
                    'SELECT COUNT(*) FROM users WHERE role=? AND enabled=1', ('admin',)
            ).fetchone()[0] <= 1:
                raise ValueError('不能停用唯一启用的管理员')
            db.execute('UPDATE users SET enabled=0 WHERE id=?', (identity,))
            db.execute('DELETE FROM sessions WHERE user_id=?', (identity,))
        self.audit(actor, 'disable_account', identity, 'success')

    def change_password(self, identity, previous, password):
        if not isinstance(previous,str) or len(previous)>128 or not isinstance(password,str) or not 12<=len(password)<=128:
            raise ValueError('新密码需为12至128个字符')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM users WHERE id=? AND enabled=1',(identity,)).fetchone()
            if not row or not hmac.compare_digest(self.password_hash(previous,row['salt']),row['password_hash']):
                raise AccessDenied('当前密码不正确')
            salt=secrets.token_hex(16)
            db.execute('UPDATE users SET salt=?,password_hash=? WHERE id=?',(salt,self.password_hash(password,salt),identity))
            db.execute('DELETE FROM sessions WHERE user_id=?',(identity,))
        self.audit(identity,'change_password','', 'success')

    def audit(self, user, action, target, outcome):
        with self.db() as db:
            db.execute('INSERT INTO audit(at,user_id,action,target,outcome) VALUES (?,?,?,?,?)',
                       (self.clock(), user, action[:60], str(target)[:160], str(outcome)[:100]))

    def recent_audit(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('''SELECT audit.*, users.display_name FROM audit
                LEFT JOIN users ON users.id=audit.user_id ORDER BY audit.id DESC LIMIT 200''')]

    def offline_reset_admin_password(self, target, newpassword):
        """Maintenance-only reset for an existing admin; no account is created."""
        return self._replace_password(target, newpassword, 'offline_admin_reset', None,
                                      require_admin=False, admin_only_target=True)


def recover_admin_password(root, target):
    """Prompt for a new password without accepting it as a command-line argument."""
    password = getpass.getpass('新管理员密码（至少12位，不显示）: ')
    confirmation = getpass.getpass('再次输入新管理员密码: ')
    if password != confirmation:
        raise ValueError('两次密码不一致')
    root = Path(root).resolve()
    if not (root / 'accounts.sqlite3').is_file():
        raise ValueError('多人账号数据库不存在，未执行恢复')
    accounts = Accounts(root)
    account = accounts.offline_reset_admin_password(target, password)
    print('管理员密码已重置，请重新登录。')
    return account


def _main():
    parser = argparse.ArgumentParser(description='本机管理员密码恢复工具')
    parser.add_argument('--data-root', required=True, help='多人服务数据目录')
    parser.add_argument('--target', required=True, help='已有管理员账号ID（32位小写十六进制）')
    args = parser.parse_args()
    recover_admin_password(args.data_root, args.target)


if __name__ == '__main__':
    _main()
