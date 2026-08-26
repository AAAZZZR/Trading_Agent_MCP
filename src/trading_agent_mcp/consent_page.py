"""OAuth 同意頁的品牌版本 —— 把 fastmcp 的預設樣板換成 Livermore 的臉。

# 為什麼要動這一頁

`/consent` 是整個產品**唯一**由我們自己渲染、又一定會被終端使用者看到的頁面:
`claude mcp add` 之後彈出的瀏覽器,第一站就是它。fastmcp 的預設樣板長得像框架的
除錯頁 —— 純白底、標題寫死 "Application Access Request"、掛 FastMCP 自己的 logo,
server 名稱直接印 `FastMCP.name`(在我們這裡曾經是內部代號 `investor-db`)。
使用者在這一頁要做的是一個**安全決定**(要不要讓某個 app 連進自己的帳號),
而一個看起來像別人家後台的頁面拿不到那份信任。

# 只換簡報層,不碰安全邏輯

fastmcp 把「產生同意頁 HTML」收斂成單一函式 `create_consent_html`;交易狀態、
CSRF token 的簽發與雙重提交驗證、cookie 綁定全都留在 `ConsentMixin` 裡。所以這裡
的做法是**只替換那一個函式**(見 `install`),安全邏輯一行都不碰 —— 我們拿到的是
已經簽好的 `txn_id` / `csrf_token`,責任只剩兩個:

  1. 表單欄位與 `ConsentMixin._submit_consent` 讀的完全一致(見 `_FORM_FIELDS`);
  2. 每一個外來字串都跑過 `html.escape`。

第 2 點不是形式:`client_name` 是**攻擊者可控**的 —— 任何人都能 DCR 註冊一個叫
`<img src=x onerror=...>` 的 client,漏一個 escape 就等於把 XSS 放在自家網域上,
而且就放在使用者正要按「允許」的那一頁。

# 為什麼不用網頁字型

同意頁的 CSP 是 `default-src 'none'`,沒有 `font-src`(fastmcp 的預設,我們沿用)。
要載 Google Fonts 就得在一個處理授權的頁面上為了字型把 CSP 開洞,是壞交易。所以
品牌感靠**色票 + 版面 + 那顆像素 logo** 撐,字型走系統堆疊 —— landing 草稿用的
Archivo / Silkscreen 在這裡刻意不跟。

logo 用 data URI 內嵌(CSP 的 `img-src` 允許 `data:`),不必多一個對外請求:這台
的出口連 claude.ai 都會被 Cloudflare 擋(見 `oauth.py` 裡 `enable_cimd=False` 的
註解),少一個外部依賴就少一個會在最糟的時候壞掉的東西。
"""

from __future__ import annotations

import html
import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

# `ConsentMixin._submit_consent` 實際會讀的欄位。改樣板時這三個名字動不得 ——
# 少一個或拼錯,使用者按下按鈕只會拿到 400,而且是靜默的(表單本身不會抱怨)。
_FORM_FIELDS = ("txn_id", "csrf_token", "action")

# 品牌色票 —— 與 landing 草稿(`Invest/livermore_landing_draft.html`)同一組:
# 深墨底 + 骨白字 + 單一金色 accent。圓角 2px 是刻意的:整組視覺走「報價單 / 紙帶」
# 的硬邊感,不是 SaaS 的圓潤感。
_INK = "#0F131A"
_SURFACE = "#1E2532"
_LINE = "#2A3242"
_BONE = "#E9E5DB"
_MUTED = "#8A93A5"
_DIM = "#5E6779"
_GOLD = "#D9A94A"
_GOLD_DEEP = "#A97F2C"

# Jesse Livermore 像素頭像(`Invest/brand/livermore-punk-t-192.png`,透明底)。
# 內嵌成 data URI 而不是讀檔:Docker image 只 `COPY src ./src`,非 .py 的資產會不會
# 進 wheel 取決於打包設定 —— 一個要到 prod 才會發現的失敗模式,不值得省這 1.7KB。
# 要換圖:跑 `Invest/brand/livermore-punk-source.py` 重產,再 base64 貼回來。
_LOGO_DATA_URI = "data:image/png;base64," + (
    "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAE8UlEQVR4nO3dv4sdVRgG4FHSKxa7"
    "kCKKCSHKBquwFgGRZLFYIUZQg4JgJUIaWwtBC1ubgKQSlEhQMAbcQrIphC1cUkkWlWAkSRHYLUT/"
    "gljPoDsZZ+49d+Z9nm64N+ee2czLd86c+VFVAAAAAAAAAAAAAADA2DxSugOl7f769YPSfShp+ZnX"
    "o4+BR0t3AEoSAKIJANEEgGgCQDQBIJoAEO1A6Q6Uduf2TukuUJAKQDQBIJoAEC1+DvDU4ZXSXaAg"
    "FYBoAkA0ASBa9LXgVVVVS0sHo+8H2Nu7H30MqABEEwCiCQDRBIBoAkA0ASCaABBNAIgmAEQTAKIJ"
    "ANEEgGgCQDQBIJoAEK34teDpz+dPV/r9BCoA0QSAaAJANAEgmgAQTQCIJgBEK74O0Gb7+w+tE4zY"
    "6ssfL/QxpgIQTQCIJgBEEwCiCQDRBIBoAkC0hT5H+zBuXLlgnaCgE2fPj/oYUgGIJgBEEwCiCQDR"
    "BIBoAkA0ASDagdId6OvQkWOlu8CIqQBEEwCiCQDRBIBoAkA0ASCaABBNAIgmAEQTAKIJANEEgGgC"
    "QDQBIJoAEG309wN0dfzU24O2d/P6F4O219XU9mfeVACiCQDRBIBok58DNMfId//YGbT9J59eqW3P"
    "egw9tf0pTQUgmgAQTQCINvo5wL3ff6ttr7/7SW176DFyU7P9ocfQsx7zN7Xtz8bFD2b6+/OmAhBN"
    "AIgmAEQb/Rygr8PPPt/p+7d/+anT94e+VqfNrPdnalQAogkA0QSAaKObAywtHay9F7h53n/RDL0O"
    "UFrz7938/9jbuz+q9warAEQTAKIJANFGNwdojjFvXLkwqjnB2DWvBTpx9vyoxvxNKgDRBIBoAkC0"
    "0c0Bhja1a2Gmtj+zpgIQTQCIJgBEm9wcoHntTfOe1rE9R2fR9qd5D/bYqQBEEwCiCQDRJjcHaGob"
    "Qw/dfpu2MfShI8c6/V7p/Rk7FYBoAkA0ASDaqK/lrqqq2r25+aD9W4uj7xxg0SwfPz3qY0gFIJoA"
    "EE0AiDb5dYB5a47x+47ph26POhWAaAJANAEgmjnAwJpj9K3NHzr9+3t37ta2T55+qXef+G8qANEE"
    "gGgCQDRzgBkzhl9sKgDRBIBoAkA0ASCaABBNAIgmAEQb9f2cD2Poe4ab1/ZsbmwP2Xxnp9dXa9tD"
    "rzuM/Z7fNioA0QSAaAJAtEmP76pq/s8N6nr9f1fzvrbIHAAmTACIJgBEm/T47mH0nSOUfmdW3+cE"
    "TX2M30YFIJoAEE0AiOae4J48q3PcVACiCQDRBIBoxc8Bnzx1ZqHf8XXt6uf7fr5z7as59eTfray9"
    "ue/na2femVNP/p+t61eLHoMqANEEgGgCQDTrAAM7+sKrte1bP367UO1RpwIQTQCIJgBEm/k52EU/"
    "z9/Xxjef7fv5re/O9Wr/6CuX9/18/bX3erW/6Ga9TqACEE0AiCYARLMO0FHbmH/emv2Z+pxgaCoA"
    "0QSAaAJAtN5zgKmf5+/qy49erG2vPrfcq73mOkLbusDUtB1ffdcJVACiCQDRBIBo8esAf//1Z6fv"
    "n1x7o7b92ONP1LY/favfmL9p++fd2vb7l+rn+bv2v6nZ/zQqANEEgGgCQLTWc6jO83dzbmW27wu4"
    "vONZpF20rROoAEQTAKIJANFmvg7Q9zz12FzcWprxL2T9PWe9TqECEE0AiCYARPsHAvnr05n+7PwA"
    "AAAASUVORK5CYII="
)

# 品牌 slogan。放在 wordmark 底下一行、字級壓到 11.5px —— 這一頁的主角是那個安全
# 決定,slogan 的工作只是讓人認出「這是我註冊的那家」,不是在這裡賣東西。
_TAGLINE = "Less noise, better choice."

# Google 那三個 scope 的人話。同意頁上印一串 googleapis.com 的 URL 只會讓人直接
# 按下一步 —— 看不懂的東西不構成知情同意。原始值仍然完整列在「Details」裡。
_SCOPE_LABELS = {
    "openid": "Confirm who you are",
    "https://www.googleapis.com/auth/userinfo.email": "Your email address",
    "https://www.googleapis.com/auth/userinfo.profile": "Your name and profile picture",
}

# 與 fastmcp `create_page` 的預設**完全相同** —— 我們換的是外觀,不是安全姿態。
# `img-src` 收 `data:` 是 logo 需要的,而它本來就在預設值裡。
_DEFAULT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; base-uri 'none'"
)

_STYLES = f"""
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
        margin: 0;
        padding: 48px 20px;
        min-height: 100vh;
        background: {_INK};
        color: {_BONE};
        font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
        font-size: 15px;
        line-height: 1.55;
        display: flex;
        flex-direction: column;
        align-items: center;
    }}
    .brand {{
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 28px;
    }}
    .brand img {{
        width: 40px;
        height: 40px;
        border-radius: 2px;
        image-rendering: pixelated;
    }}
    .wordmark {{
        display: block;
        font-size: 19px;
        font-weight: 700;
        letter-spacing: 0.18em;
        color: {_BONE};
        text-decoration: none;
    }}
    .tagline {{
        margin: 3px 0 0;
        font-size: 11.5px;
        letter-spacing: 0.04em;
        color: {_DIM};
    }}
    .card {{
        width: 100%;
        max-width: 480px;
        background: {_SURFACE};
        border: 1px solid {_LINE};
        border-radius: 2px;
        padding: 32px;
    }}
    h1 {{
        margin: 0 0 6px;
        font-size: 21px;
        font-weight: 700;
        letter-spacing: -0.01em;
    }}
    .lede {{ margin: 0 0 24px; color: {_MUTED}; }}
    .lede strong {{ color: {_BONE}; font-weight: 600; }}
    .callback {{
        border: 1px solid {_GOLD_DEEP};
        border-left-width: 3px;
        border-radius: 2px;
        padding: 12px 14px;
        margin-bottom: 20px;
        background: rgba(217, 169, 74, 0.06);
    }}
    .callback .label {{
        display: block;
        font-size: 11px;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: {_GOLD};
        margin-bottom: 5px;
    }}
    .callback .value {{
        font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
        font-size: 13px;
        color: {_BONE};
        word-break: break-all;
    }}
    .callback .hint {{ margin: 7px 0 0; font-size: 12.5px; color: {_MUTED}; }}
    .scopes {{ margin: 0 0 24px; padding: 0; list-style: none; }}
    .scopes li {{
        display: flex;
        gap: 9px;
        padding: 4px 0;
        color: {_MUTED};
        font-size: 14px;
    }}
    .scopes .tick {{ color: {_GOLD}; }}
    details {{ margin-bottom: 24px; }}
    summary {{
        cursor: pointer;
        font-size: 13px;
        color: {_MUTED};
        list-style: none;
    }}
    summary::-webkit-details-marker {{ display: none; }}
    .detail-row {{
        display: grid;
        grid-template-columns: 112px 1fr;
        gap: 10px;
        padding: 7px 0;
        border-top: 1px solid {_LINE};
        font-size: 12.5px;
    }}
    .detail-row:first-of-type {{ border-top: none; padding-top: 12px; }}
    .detail-label {{ color: {_DIM}; }}
    .detail-value {{
        font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
        color: {_MUTED};
        word-break: break-all;
    }}
    .buttons {{ display: flex; gap: 10px; }}
    button {{
        flex: 1;
        padding: 11px 16px;
        border-radius: 2px;
        font-size: 14.5px;
        font-weight: 600;
        font-family: inherit;
        cursor: pointer;
        border: 1px solid transparent;
    }}
    .approve {{ background: {_GOLD}; color: {_INK}; border-color: {_GOLD}; }}
    .approve:hover {{ background: {_GOLD_DEEP}; border-color: {_GOLD_DEEP}; color: {_BONE}; }}
    .deny {{ background: transparent; color: {_MUTED}; border-color: {_LINE}; }}
    .deny:hover {{ color: {_BONE}; border-color: {_DIM}; }}
    .why {{
        max-width: 480px;
        margin: 20px 0 0;
        font-size: 12.5px;
        line-height: 1.6;
        color: {_DIM};
        text-align: center;
    }}
    .why a {{ color: {_MUTED}; }}
"""


def _scope_items(scopes: list[str]) -> str:
    """把 scope 清單畫成人話。不認得的 scope 原樣印出 —— 猜不得。"""
    if not scopes:
        return ""
    rows = "".join(
        f'<li><span class="tick">&#x2713;</span>'
        f"<span>{html.escape(_SCOPE_LABELS.get(scope, scope))}</span></li>"
        for scope in scopes
    )
    return f'<ul class="scopes">{rows}</ul>'


def _detail_rows(rows: list[tuple[str, str]]) -> str:
    return "".join(
        f'<div class="detail-row"><div class="detail-label">{html.escape(label)}</div>'
        f'<div class="detail-value">{html.escape(value)}</div></div>'
        for label, value in rows
    )


def render_consent_page(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    txn_id: str,
    csrf_token: str,
    client_name: str | None = None,
    server_name: str | None = None,
    server_website_url: str | None = None,
    csp_policy: str | None = None,
    **_ignored: Any,
) -> str:
    """畫出同意頁。

    簽名刻意收 `**_ignored`:fastmcp 是用關鍵字呼叫的,它未來多傳一個參數
    (像 `is_cimd_client` 那樣中途長出來的)不該讓整頁 500 —— 多出來的資訊沒畫
    只是少一塊裝飾,拋例外卻會讓人登不進來。

    Args:
        client_id / client_name: 要連進來的 client。**兩者都是外來輸入**——
            任何人都能拿任意名字跑 DCR,一律 escape。
        redirect_uri: 授權碼會被送去哪。這是使用者唯一能據以判斷「這是不是我
            自己發起的」的線索,所以擺在最顯眼的位置而不是收進摺疊區。
        scopes: 要授權的範圍。
        txn_id / csrf_token: fastmcp 簽好的,原樣塞回表單。
        server_name / server_website_url: 來自 `FastMCP(name=..., website_url=...)`。
        csp_policy: `None` = 用 fastmcp 的預設;`""` = 完全不下 CSP meta;
            其餘字串照用。語意與 `create_page` 對齊。
    """
    client_display = html.escape(client_name or client_id)
    server_display = html.escape(server_name or "this server")
    redirect_display = html.escape(redirect_uri)

    if server_website_url:
        href = html.escape(server_website_url, quote=True)
        wordmark = (
            f'<a class="wordmark" href="{href}" target="_blank" '
            f'rel="noopener noreferrer">{server_display.upper()}</a>'
        )
    else:
        wordmark = f'<span class="wordmark">{server_display.upper()}</span>'

    details = _detail_rows(
        [
            ("Application", client_name or client_id),
            ("Application ID", client_id),
            ("Redirect URI", redirect_uri),
            ("Scopes", ", ".join(scopes) if scopes else "None"),
        ]
    )

    policy = _DEFAULT_CSP if csp_policy is None else csp_policy
    csp_meta = (
        f'<meta http-equiv="Content-Security-Policy" '
        f'content="{html.escape(policy, quote=True)}" />'
        if policy
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Authorize &middot; {server_display}</title>
{csp_meta}
<style>{_STYLES}</style>
</head>
<body>
    <div class="brand">
        <img src="{_LOGO_DATA_URI}" alt="" width="40" height="40">
        <div>
            {wordmark}
            <p class="tagline">{_TAGLINE}</p>
        </div>
    </div>
    <div class="card">
        <h1>Authorize access</h1>
        <p class="lede"><strong>{client_display}</strong> wants to connect to your
            {server_display} account and read market data on your behalf.</p>

        <div class="callback">
            <span class="label">Credentials will be sent to</span>
            <span class="value">{redirect_display}</span>
            <p class="hint">Only continue if you started this yourself and recognise
                this address.</p>
        </div>

        {_scope_items(scopes)}

        <details>
            <summary>Details</summary>
            {details}
        </details>

        <form method="POST" action="">
            <input type="hidden" name="txn_id" value="{html.escape(txn_id, quote=True)}">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
            <input type="hidden" name="submit" value="true">
            <div class="buttons">
                <button type="submit" name="action" value="approve" class="approve">Allow access</button>
                <button type="submit" name="action" value="deny" class="deny">Deny</button>
            </div>
        </form>
    </div>
    <p class="why">This screen exists so a client can&#39;t quietly authorize itself in your
        name &mdash; a <a href="https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices#confused-deputy-problem"
        target="_blank" rel="noopener noreferrer">confused deputy</a> attack.</p>
</body>
</html>"""


def install() -> bool:
    """把 fastmcp 的同意頁換成上面這一版;回傳有沒有換成功。

    做法是替換 `fastmcp.server.auth.oauth_proxy.consent` 模組裡的
    `create_consent_html` 這個名字 —— `ConsentMixin._show_consent` 就是從那裡查的。
    這確實是在動第三方模組的內部,所以**先驗簽名再換**:fastmcp 哪天改了那組參數
    (它自己把這一帶的 CIMD 標為 beta,還在動),我們寧可退回它的預設頁,也不要
    換上一個渲染到一半就爆的頁面把登入弄壞。**醜 > 壞。**

    Returns:
        True = 已套用品牌頁;False = 簽名對不上,維持 fastmcp 預設(並留 error log)。
    """
    from fastmcp.server.auth.oauth_proxy import consent as fastmcp_consent

    required = {"client_id", "redirect_uri", "scopes", "txn_id", "csrf_token"}
    try:
        params = set(inspect.signature(fastmcp_consent.create_consent_html).parameters)
    except (TypeError, ValueError):  # pragma: no cover —— 防呆,正常拿得到簽名
        logger.error("cannot inspect fastmcp create_consent_html; keeping default page")
        return False

    missing = required - params
    if missing:
        logger.error(
            "fastmcp create_consent_html no longer accepts %s; keeping the default "
            "consent page (branding skipped)",
            sorted(missing),
        )
        return False

    fastmcp_consent.create_consent_html = render_consent_page
    logger.info("branded consent page installed")
    return True
