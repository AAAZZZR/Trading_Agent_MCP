"""SaaS 控制面 DB(api_keys / subscriptions / usage_events)的 asyncpg 連線池。

為什麼 MCP 直接讀 DB,而不是像過去那樣繞一趟後端 API:
  舊模型每個 HTTP 請求都要同步呼叫一次 API 才能驗 key,鏈上任何一段抖動
  (API redeploy、DB 慢、ingress 掐 keepalive)都會被翻譯成 401,而 401 對 MCP
  client 的意思是「你的憑證壞了」—— 使用者明明存了正確的 key 卻被反覆踢掉。
  少一跳(MCP → DB)就少一段可壞的鏈,而且驗 key 本來就只是一個 indexed lookup。

設計重點(與 `db.py` 的 readonly pool 同一套路,但**不同 DSN、不同權限**):

- Lazy init:第一次要驗 key 才建 pool;沒設 `MCP_SAAS_DATABASE_URL` 整個模組不會炸,
  只在被呼叫時丟 `SaasDBNotConfigured`(呼叫端當成「暫時性錯誤」處理)。
- Module-level singleton + `asyncio.Lock`:多個請求同時進來只會建一個 pool。
- 建 pool 失敗要把 singleton 重設回 None —— 否則一個半殘的 pool 物件會被永久快取,
  DB 復原後也永遠連不回去。
- 每個 query 都帶 timeout:DB 卡住不能讓 MCP 請求跟著卡,寧可快速失敗、
  讓 auth 層落回正向快取(見 `auth.py`)。

這個 DSN 需要寫入權限(INSERT usage_events / UPDATE api_keys.last_used_at /
INSERT users),所以不能沿用 `MCP_READONLY_DB_DSN` 的唯讀 role。

OAuth(Google 登入)路徑另外用到 `upsert_google_user` —— 把 Google 身分換成我們自己的
`users.id`,兩條認證路徑(bearer API key / OAuth session)之後才共用同一個 user 空間。
"""

from __future__ import annotations

import asyncio

import asyncpg

from trading_agent_mcp.settings import settings

# 沒有 subscriptions 列 = 免費方案(API repo billing_config.py 的同一約定)。
# 當成 SQL 參數傳進去,不在 SQL 字面值裡寫死,改預設方案時只要動這一行。
DEFAULT_TIER = "free"

# 單一 query 的上限秒數。auth 路徑在每個 MCP 請求的關鍵路徑上,DB 慢一秒就是
# 使用者等一秒;3 秒遠超過一個 indexed lookup 該花的時間,超過就代表 DB 有事,
# 該讓 auth 層落回快取而不是陪它一起卡。
QUERY_TIMEOUT_S = 3.0

# 建立連線的上限秒數。asyncpg 預設 60 秒 —— DB 不可達時第一個請求會整整卡一分鐘,
# 對「驗個 key」來說完全不能接受。
POOL_CONNECT_TIMEOUT_S = 5.0

# Pool 大小 —— 每個請求只跑一兩個極短的 query,連線數不需要多。
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5

# `api_keys.last_used_at` 的寫入節流秒數。這個欄位只是給使用者看「這把 key 最近
# 有在用」,精度到分鐘就夠;每次呼叫都寫 = 每次呼叫都鎖那一列(高頻 agent 會自己
# 跟自己搶鎖)。API repo 的舊 SQL 就是這個節流,沿用同一口徑。
LAST_USED_THROTTLE = "5 minutes"

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


class SaasDBNotConfigured(RuntimeError):
    """`MCP_SAAS_DATABASE_URL` 沒設 —— per-user 認證無法運作。

    呼叫端(auth.py)把它併進「暫時性 DB 錯誤」處理:沒快取就回 401,
    而不是讓整個 process 在 import 期就炸掉。
    """


# key_hash 是 UNIQUE index,所以這是單列 index lookup。
# tier 用 LEFT JOIN + COALESCE 一次帶回來:沒有 subscriptions 列 = free
# (SaaS 側只有付費才會寫入那張表),不必為了 tier 再跑第二個 query。
_LOOKUP_KEY_SQL = """
SELECT k.id AS api_key_id,
       k.user_id::text AS user_id,
       k.revoked_at,
       COALESCE(s.tier, $2) AS tier
FROM api_keys k
LEFT JOIN subscriptions s ON s.user_id = k.user_id
WHERE k.key_hash = $1
"""

# 滾動 24 小時,不是自然日 —— 跟 API repo 的配額 SQL 同一口徑,兩邊算出來的
# used_today 才會一致。
_COUNT_USAGE_SQL = """
SELECT count(*)
FROM usage_events
WHERE user_id = $1::uuid
  AND surface = 'mcp'
  AND ts >= now() - interval '24 hours'
"""

# 計量與 last_used_at 合成一個 statement:兩件事都發生在同一個「呼叫了 tool」
# 事件上,拆成兩次 execute 等於兩次 round-trip + 兩次 acquire,而它們之間沒有
# 任何順序或原子性需求 —— 用 data-modifying CTE 一次送完最省。
_RECORD_USAGE_SQL = f"""
WITH logged AS (
    INSERT INTO usage_events (user_id, api_key_id, surface, action)
    VALUES ($1::uuid, $2::uuid, 'mcp', $3)
)
UPDATE api_keys
   SET last_used_at = now()
 WHERE id = $2::uuid
   AND (last_used_at IS NULL OR last_used_at < now() - interval '{LAST_USED_THROTTLE}')
"""


# Google 身分 → 我們自己的 users 列。**一次 round-trip** 做完三段語意
# (對齊 API repo `api/routers/auth.py:_upsert_google_user`,同一批 users 表):
#   1. `by_sub` —— 這個 Google 帳號登入過 → `bound` 只補**空的**顯示資訊。
#      刻意用 COALESCE(users.x, EXCLUDED.x) 而不是反過來:使用者可能在別處改過
#      自己的名字 / 頭像,每次登入都拿 Google 的蓋掉會讓那些修改無聲消失。
#   2. `created` —— 沒登入過 → INSERT。`ON CONFLICT (email)` 有兩個作用:
#      (a) 同一個人先用 email/密碼註冊、後來改用 Google 登入 → 綁定既有那列;
#      (b) 冪等:同一個新使用者的兩個請求同時進來(agent 開連線時很常見),
#          後到的那個不會爆 UniqueViolation,而是走 DO UPDATE 拿回同一個 id。
#      DO UPDATE 的 `WHERE users.google_sub IS NULL OR = EXCLUDED.google_sub`
#      是安全閘:email 已經綁在**別的** Google 帳號上時,DO UPDATE 不動任何列、
#      整個查詢回 0 列,呼叫端據此拒絕(fail closed)。這種情況只可能發生在
#      Workspace 回收 email 之類的邊緣狀況,寧可讓人重新註冊也不要換人登入成功。
# tier 一樣用 LEFT JOIN + COALESCE 帶回來(無 subscriptions 列 = free),
# 省掉第二個 query —— 與 `_LOOKUP_KEY_SQL` 同一口徑。
_UPSERT_GOOGLE_USER_SQL = """
WITH by_sub AS (
    SELECT id FROM users WHERE google_sub = $1
),
bound AS (
    UPDATE users
       SET name = COALESCE(users.name, $3),
           avatar_url = COALESCE(users.avatar_url, $4)
     WHERE id = (SELECT id FROM by_sub)
    RETURNING id
),
created AS (
    INSERT INTO users (email, google_sub, name, avatar_url)
    SELECT $2::text, $1::text, $3::text, $4::text
     WHERE NOT EXISTS (SELECT 1 FROM by_sub)
    ON CONFLICT (email) DO UPDATE
       SET google_sub = EXCLUDED.google_sub,
           name       = COALESCE(users.name, EXCLUDED.name),
           avatar_url = COALESCE(users.avatar_url, EXCLUDED.avatar_url)
     WHERE users.google_sub IS NULL OR users.google_sub = EXCLUDED.google_sub
    RETURNING id
),
resolved AS (
    SELECT id FROM bound
    UNION ALL
    SELECT id FROM created
)
SELECT r.id::text AS user_id, COALESCE(s.tier, $5) AS tier
FROM resolved r
LEFT JOIN subscriptions s ON s.user_id = r.id
"""


async def get_pool() -> asyncpg.Pool:
    """取(或第一次建)SaaS 控制面 pool。沒設 DSN → SaasDBNotConfigured。"""
    global _pool
    if not settings.mcp_saas_database_url:
        raise SaasDBNotConfigured(
            "MCP_SAAS_DATABASE_URL is not set; per-user authentication is disabled."
        )
    if _pool is not None:
        return _pool
    async with _pool_lock:
        # 雙重檢查:等鎖的期間可能已經有人建好了。
        if _pool is None:
            try:
                # 先建到 local、成功才指派給 singleton —— 建 pool 失敗時 _pool 必須
                # 保持 None,否則一個半殘的 pool 物件會被永久快取住,DB 復原之後
                # 也再也不會重建。except 分支把這個保證寫死(連 CancelledError,
                # 也就是請求中途被取消的情況,也要清乾淨)。
                pool = await asyncpg.create_pool(
                    dsn=settings.mcp_saas_database_url,
                    min_size=_POOL_MIN_SIZE,
                    max_size=_POOL_MAX_SIZE,
                    command_timeout=QUERY_TIMEOUT_S,
                    timeout=POOL_CONNECT_TIMEOUT_S,
                )
            except BaseException:
                _pool = None
                raise
            _pool = pool
    return _pool


async def close_pool() -> None:
    """測試 / shutdown 用 —— 關掉 pool 並重設 singleton。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def lookup_key(key_hash: str) -> asyncpg.Record | None:
    """用 key 的 sha256 hash 查回 api_key_id / user_id / revoked_at / tier。

    查 hash 不查明文:`api_keys.key_hash` 才有 UNIQUE index,而且 DB 側本來就
    以 hash 為準(API repo `saas/auth.py:hash_key`)。

    Returns:
        找到 → Record;key 不存在 → None(呼叫端據此回 401)。
        `revoked_at` 非 None 代表已撤銷,由呼叫端判斷(這裡不過濾,好讓
        「撤銷」與「不存在」在 log 上區分得開)。
    """
    pool = await get_pool()
    # acquire 也要限時:pool 滿載或 DB 不可達時,不限時的 acquire 會無聲卡住,
    # query 的 timeout 根本輪不到生效。逾時丟 asyncio.TimeoutError,呼叫端當
    # 暫時性錯誤處理。
    async with pool.acquire(timeout=QUERY_TIMEOUT_S) as conn:
        return await conn.fetchrow(_LOOKUP_KEY_SQL, key_hash, DEFAULT_TIER, timeout=QUERY_TIMEOUT_S)


async def count_usage_24h(user_id: str) -> int:
    """該 user 在滾動 24 小時內的 MCP 呼叫次數(surface='mcp')。"""
    pool = await get_pool()
    async with pool.acquire(timeout=QUERY_TIMEOUT_S) as conn:
        return await conn.fetchval(_COUNT_USAGE_SQL, user_id, timeout=QUERY_TIMEOUT_S)


async def upsert_google_user(
    google_sub: str,
    email: str,
    name: str | None,
    avatar_url: str | None,
) -> asyncpg.Record | None:
    """把一個 Google 身分換成我們自己的 user_id + tier(必要時建帳號)。

    OAuth 路徑專用:`StockfactsGoogleProvider` 驗完 Google token 之後呼叫,
    因為 `usage_events.user_id` 這類 FK 認的是我們的 uuid,不是 Google 的 sub。

    語意細節(三段合併成一次 round-trip、為什麼只補不覆蓋、email 撞到別的
    Google 帳號為什麼 fail closed)全部寫在 `_UPSERT_GOOGLE_USER_SQL` 上面。

    Args:
        google_sub: Google id_token 的 `sub`(該 Google 帳號的永久識別碼)。
        email: 已驗證的 email —— 呼叫端必須先確認 `email_verified`,這裡不再檢查。
        name / avatar_url: 顯示用資訊,可為 None(只在既有值為空時才補上)。

    Returns:
        Record(`user_id` text / `tier` text);
        None 代表這個 email 已經綁在**別的** Google 帳號上 —— 呼叫端要拒絕放行。
    """
    pool = await get_pool()
    async with pool.acquire(timeout=QUERY_TIMEOUT_S) as conn:
        return await conn.fetchrow(
            _UPSERT_GOOGLE_USER_SQL,
            google_sub,
            email,
            name,
            avatar_url,
            DEFAULT_TIER,
            timeout=QUERY_TIMEOUT_S,
        )


async def record_usage(user_id: str, api_key_id: str | None, action: str) -> None:
    """記一筆 MCP 用量,並(節流地)更新這把 key 的 last_used_at。

    Args:
        user_id: uuid 字串。
        api_key_id: uuid 字串;**OAuth session 沒有 API key,傳 None**。
            該欄位本來就 nullable(FK 是 ON DELETE SET NULL),而 SQL 裡的
            `UPDATE api_keys ... WHERE id = $2::uuid` 在 NULL 時自然不匹配任何列,
            所以同一個 statement 兩條路都適用,不必分岔。
        action: tool 名稱 —— 舊模型只記得到 "connect"(框架驗 token 時還不知道
            要呼叫哪個 tool),改在元件層計量之後才拿得到真正的粒度。
    """
    pool = await get_pool()
    async with pool.acquire(timeout=QUERY_TIMEOUT_S) as conn:
        await conn.execute(_RECORD_USAGE_SQL, user_id, api_key_id, action, timeout=QUERY_TIMEOUT_S)
