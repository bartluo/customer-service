"""测试环境统一配置（必须在导入应用之前执行）。

为什么需要：
  · 应用运行在容器里时，依赖主机名是 postgres / qdrant / redis / embedding；
    在开发机或 CI 上这些名字无法解析，会让每次探活都等 DNS 超时。
  · 测试只验证"接口契约与降级行为"，不依赖真实依赖，因此统一指向 127.0.0.1
    并缩短探活超时，保证任何环境下都快速、结果确定。
"""

import os

os.environ["DATABASE_URL"] = (
    "postgresql+psycopg://cs_user:cs_password@127.0.0.1:5432/customer_service"
)
os.environ["QDRANT_URL"] = "http://127.0.0.1:6333"
# 测试用的集合单独加前缀。不隔离的话，"清空集合再灌数据"的用例会把
# 开发环境里真实的向量索引删光——跑一次测试，检索就查不到东西了。
os.environ["QDRANT_COLLECTION_PREFIX"] = "test_"
# 任务队列也要隔离：复核接口会往 Celery 投"重建索引"任务，
# 如果投到真实的 broker，运行中的 worker 会领走并在真实数据上执行——
# 出现过一次：测试跑完，开发环境的向量索引被清空了。
# 用 15 号库（默认 0 号），测试投的任务没人消费，随 Redis 自然过期。
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/15"
os.environ["EMBEDDING_URL"] = "http://127.0.0.1:8001"
os.environ["HEALTH_CHECK_TIMEOUT_SECONDS"] = "0.5"
os.environ["DB_CONNECT_TIMEOUT_SECONDS"] = "1"
os.environ["FEATURE_PLANNING_ENABLED"] = "false"
os.environ["ENVIRONMENT"] = "test"

# 固定管理员口令必须在这里设好：应用模块在导入时就会缓存配置，
# 等测试 fixture 再改环境变量已经太晚，管理员会被建成随机口令。
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "Fixed-Admin-Pass-2026"
os.environ["JWT_SECRET"] = "test-secret-for-account-tests"
