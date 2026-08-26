"""品牌同意頁的測試。

這一頁沒有商業邏輯,但有兩件錯了會很痛的事,所以測試集中在它們身上:

  1. **表單契約** —— 頁面是我們畫的,收表單的是 fastmcp。欄位名對不上,使用者按
     「Allow access」只會拿到 400,而且沒有任何錯誤訊息指向這裡。
  2. **跳脫** —— `client_name` / `client_id` 是攻擊者可控的(任何人都能 DCR 註冊
     一個名字任取的 client),漏跳脫就是把 XSS 掛在自家授權頁上。

另外測 `install()` 的**護欄**:fastmcp 換了簽名時要退回它的預設頁,不能換上一個
會爆的頁面 —— 醜 > 壞。
"""

import inspect

import pytest
from fastmcp.server.auth.oauth_proxy import consent as fastmcp_consent
from fastmcp.server.auth.oauth_proxy import ui as fastmcp_ui

from trading_agent_mcp.consent_page import (
    _FORM_FIELDS,
    _TAGLINE,
    install,
    render_consent_page,
)

_BASE_KWARGS = {
    "client_id": "9c1f0f6e-0000-4b6a-9f3a-7d2c1b5e4a10",
    "redirect_uri": "http://localhost:3118/callback",
    "scopes": [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
    ],
    "txn_id": "txn-abc",
    "csrf_token": "csrf-xyz",
    "client_name": "Claude Code",
    "server_name": "Livermore",
    "server_website_url": "https://livermore.club",
}


def _render(**overrides) -> str:
    return render_consent_page(**{**_BASE_KWARGS, **overrides})


@pytest.fixture(autouse=True)
def _restore_fastmcp_symbol():
    """每個測試跑完把 fastmcp 的模組屬性還原 —— install() 動的是全域狀態。"""
    original = fastmcp_consent.create_consent_html
    yield
    fastmcp_consent.create_consent_html = original


# ============================================================
# 表單契約:欄位名與 ConsentMixin._submit_consent 讀的一致
# ============================================================


@pytest.mark.parametrize("field", _FORM_FIELDS)
def test_form_carries_every_field_the_handler_reads(field) -> None:
    """`_submit_consent` 讀 txn_id / csrf_token / action,三個都得出現在表單裡。"""
    assert f'name="{field}"' in _render()


def test_form_posts_to_itself() -> None:
    """action="" = 送回同一個路徑。寫死 /consent 會在掛載到 /mcp 前綴時全壞。"""
    html = _render()
    assert 'method="POST"' in html
    assert 'action=""' in html


def test_both_decisions_are_offered() -> None:
    """核准與拒絕都要有 —— 只給「同意」的同意頁不是同意頁。"""
    html = _render()
    assert 'value="approve"' in html
    assert 'value="deny"' in html


def test_transaction_values_are_carried_through() -> None:
    """txn_id / csrf_token 原樣塞回去,否則 CSRF 雙重提交檢查必定失敗。"""
    html = _render(txn_id="TXN-42", csrf_token="CSRF-42")
    assert 'value="TXN-42"' in html
    assert 'value="CSRF-42"' in html


# ============================================================
# 跳脫:client_* 是攻擊者可控的輸入
# ============================================================


@pytest.mark.parametrize("field", ["client_name", "client_id", "redirect_uri"])
def test_hostile_input_is_escaped(field) -> None:
    """任何人都能註冊一個名字帶標籤的 client;原樣印出去就是自家網域上的 XSS。"""
    payload = '<img src=x onerror="alert(1)">'
    html = _render(**{field: payload})

    # 重點是**開不出標籤**,不是頁面上不能出現 "onerror" 這幾個字:跳脫過後它就是
    # 一串給人看的文字。所以驗的是尖括號與引號都被吃掉了。
    assert payload not in html
    # 不能寫成 `"<img" not in html` —— 品牌 logo 自己就是一個合法的 <img>。
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html


def test_hostile_website_url_cannot_break_out_of_the_href() -> None:
    """字標的 href 是屬性位置 —— 引號必須跳脫,否則可以塞出一個新屬性。"""
    html = _render(server_website_url='https://x" onmouseover="alert(1)')
    assert 'onmouseover="alert(1)"' not in html
    assert "&quot;" in html


# ============================================================
# 品牌
# ============================================================


def test_brand_is_livermore_not_the_framework() -> None:
    """標題、字標、slogan 都是我們的;不能再出現 FastMCP 的預設字串。"""
    html = _render()

    assert "<title>Authorize &middot; Livermore</title>" in html
    assert "LIVERMORE" in html
    assert _TAGLINE in html
    assert "FastMCP" not in html
    assert "Application Access Request" not in html


def test_wordmark_links_home_when_a_website_is_configured() -> None:
    assert 'href="https://livermore.club"' in _render()


def test_wordmark_is_plain_text_without_a_website() -> None:
    """沒設 website_url 就不要生一個空的連結。"""
    html = _render(server_website_url=None)
    assert '<span class="wordmark">LIVERMORE</span>' in html


def test_callback_address_is_shown_verbatim() -> None:
    """使用者判斷「這是不是我自己發起的」全靠這一行,不能省略或截斷。"""
    assert "http://localhost:3118/callback" in _render()


def test_known_scopes_are_translated_and_unknown_ones_pass_through() -> None:
    """看不懂的 scope 不構成知情同意;但不認得的也不能猜,原樣印。"""
    html = _render(scopes=["openid", "https://example.com/auth/weird.scope"])

    assert "Confirm who you are" in html
    assert "https://example.com/auth/weird.scope" in html


def test_no_scopes_renders_without_an_empty_list() -> None:
    html = _render(scopes=[])
    assert '<ul class="scopes">' not in html


def test_logo_is_inlined_not_fetched() -> None:
    """外部圖片會被 CSP 擋、也多一個對外請求(這台出口本來就不可靠)。"""
    html = _render()
    assert 'src="data:image/png;base64,' in html
    assert "http://" not in html.split("<body>")[0]


# ============================================================
# CSP:語意必須與 fastmcp create_page 對齊
# ============================================================


def test_csp_defaults_to_the_framework_policy() -> None:
    """None = 沿用 fastmcp 的預設。我們換的是外觀,不是安全姿態。"""
    html = _render(csp_policy=None)
    assert "Content-Security-Policy" in html
    assert "default-src &#x27;none&#x27;" in html


def test_empty_csp_omits_the_meta_tag() -> None:
    """空字串在 fastmcp 的語意是「完全不下 CSP」,不是「下一個空的」。"""
    assert "Content-Security-Policy" not in _render(csp_policy="")


def test_custom_csp_is_used_as_is() -> None:
    assert "default-src &#x27;self&#x27;" in _render(csp_policy="default-src 'self'")


# ============================================================
# install():護欄與實際生效
# ============================================================


def test_install_replaces_the_framework_renderer() -> None:
    assert install() is True
    assert fastmcp_consent.create_consent_html is render_consent_page


def test_install_backs_off_when_the_framework_signature_changes(caplog) -> None:
    """簽名對不上就退回預設頁 —— 寧可醜,不要把登入弄壞。"""

    def _renamed_everything(**kwargs):  # pragma: no cover —— 只是個假簽名
        return ""

    fastmcp_consent.create_consent_html = _renamed_everything

    assert install() is False
    assert fastmcp_consent.create_consent_html is _renamed_everything
    assert "keeping the default" in caplog.text


def test_we_accept_everything_the_framework_passes() -> None:
    """拿 fastmcp 原函式的參數表反過來餵我們 —— 它多傳一個參數不該讓整頁 500。

    這是這個檔案裡最重要的一條:`create_consent_html` 的參數是逐版長出來的
    (`is_cimd_client` / `cimd_domain` 就是後來才加的),而 fastmcp 全部用關鍵字
    呼叫。少收一個就是 TypeError,而且只有真人走到同意頁才會發現。
    """
    params = inspect.signature(fastmcp_ui.create_consent_html).parameters
    kwargs = {
        name: _BASE_KWARGS.get(name, [] if name == "scopes" else "x") for name in params
    }

    html = render_consent_page(**kwargs)
    assert "<title>" in html
