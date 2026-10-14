import base64
import os
import threading
from pathlib import Path

import paramiko
import pymysql
from dotenv import load_dotenv
from sqlalchemy import URL, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


# 读取 back/.env
load_dotenv()


MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "content_safety")

# 设定 SSH_HOST 后，应用会经 SSH 访问云端 MySQL；不设定时仍连接本机 MySQL。
SSH_HOST = os.getenv("SSH_HOST")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
SSH_USER = os.getenv("SSH_USER")
SSH_PASSWORD = os.getenv("SSH_PASSWORD")
SSH_PRIVATE_KEY_FILE = os.getenv("SSH_PRIVATE_KEY_FILE")
SSH_PRIVATE_KEY_PASSPHRASE = os.getenv("SSH_PRIVATE_KEY_PASSPHRASE")
SSH_HOST_KEY_FILE = os.getenv("SSH_HOST_KEY_FILE")


class SSHMySQLConnector:
    """复用 SSH 连接，并为 SQLAlchemy 的每个数据库连接打开一个 SSH 通道。"""

    def __init__(self):
        self._client: paramiko.SSHClient | None = None
        self._lock = threading.RLock()

    def _connect_ssh(self) -> paramiko.SSHClient:
        if not SSH_HOST or not SSH_USER:
            raise RuntimeError("启用 SSH 数据库连接时，必须配置 SSH_HOST 和 SSH_USER")

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if SSH_HOST_KEY_FILE:
            host_key_file = Path(os.path.expandvars(SSH_HOST_KEY_FILE)).expanduser()
            if not host_key_file.is_file():
                raise RuntimeError(f"找不到 SSH_HOST_KEY_FILE：{host_key_file}")
            key_parts = host_key_file.read_text(encoding="utf-8").split()
            if len(key_parts) < 2 or not key_parts[0].startswith("ssh-"):
                raise RuntimeError("SSH_HOST_KEY_FILE 必须是服务器的 OpenSSH 公钥文件")
            host_key = paramiko.PKey.from_type_string(
                key_parts[0], base64.b64decode(key_parts[1], validate=True)
            )
            host_name = SSH_HOST if SSH_PORT == 22 else f"[{SSH_HOST}]:{SSH_PORT}"
            client.get_host_keys().add(host_name, key_parts[0], host_key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            password=SSH_PASSWORD or None,
            key_filename=SSH_PRIVATE_KEY_FILE or None,
            passphrase=SSH_PRIVATE_KEY_PASSPHRASE or None,
            allow_agent=False,
            look_for_keys=False,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )
        transport = client.get_transport()
        if transport is None:
            client.close()
            raise RuntimeError("SSH 连接已建立，但无法取得传输通道")
        transport.set_keepalive(30)
        return client

    def connect(self):
        with self._lock:
            transport = self._client.get_transport() if self._client else None
            if transport is None or not transport.is_active():
                if self._client:
                    self._client.close()
                self._client = self._connect_ssh()
                transport = self._client.get_transport()
            channel = transport.open_channel(
                "direct-tcpip",
                (MYSQL_HOST, int(MYSQL_PORT)),
                ("127.0.0.1", 0),
                timeout=15,
            )

        connection = pymysql.connect(
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            charset="utf8mb4",
            defer_connect=True,
            # MySQL 位于 SSH 加密通道内；Paramiko Channel 不能再被 TLS 包装。
            ssl_disabled=True,
            read_timeout=30,
            write_timeout=30,
        )
        try:
            connection.connect(sock=channel)
            return connection
        except Exception:
            channel.close()
            raise

    def close(self):
        with self._lock:
            if self._client:
                self._client.close()
                self._client = None


ssh_mysql_connector = SSHMySQLConnector() if SSH_HOST else None


# 使用 URL 对象，避免密码中的 @、: 等字符破坏连接地址。
DATABASE_URL = URL.create(
    "mysql+pymysql",
    username=MYSQL_USER,
    password=MYSQL_PASSWORD,
    host=MYSQL_HOST,
    port=int(MYSQL_PORT),
    database=MYSQL_DATABASE,
    query={"charset": "utf8mb4"},
)


# 创建数据库引擎。仅在启用 SSH 时传入自定义连接创建器。
engine_options = {
    "pool_pre_ping": True,
    "echo": True,
}
if ssh_mysql_connector:
    engine_options["creator"] = ssh_mysql_connector.connect

engine = create_engine(DATABASE_URL, **engine_options)


# 每次请求可以从这里创建数据库会话
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    # 工作台聚合接口会在同一事务中推进训练进度并继续序列化已查询的对象。
    expire_on_commit=False,
)


# 所有数据库模型都继承这个基类
class Base(DeclarativeBase):
    pass


def get_db():
    """
    给 FastAPI 路由提供数据库会话。
    使用完后自动关闭。
    """

    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


def test_database_connection():
    """
    启动时测试 MySQL 是否能够连接。
    """

    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    print("MySQL 数据库连接成功")


def close_database_connection():
    """在应用退出时释放数据库连接池和 SSH 连接。"""
    engine.dispose()
    if ssh_mysql_connector:
        ssh_mysql_connector.close()
