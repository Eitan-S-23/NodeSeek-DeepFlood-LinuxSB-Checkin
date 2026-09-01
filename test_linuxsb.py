# -- coding: utf-8 --
"""linuxsb_daily 签到逻辑测试。

linuxsb_daily 只依赖 requests 与 notify，这里用 mock 替换网络请求，
使签到判断与多账号流程可以脱离真实站点独立验证。
"""
import os
import re
import unittest
from unittest import mock

import linuxsb_daily as daily


# 模拟签到页 HTML：未签到 + 含 CSRF
PAGE_UNCHECKED = (
    '<html><body>'
    '<input type="hidden" name="_csrf" value="abc123csrf">'
    '<button>每日签到</button>'
    '</body></html>'
)
# 模拟签到页 HTML：已签到
PAGE_CHECKED = (
    '<html><body>'
    '<input type="hidden" name="_csrf" value="abc123csrf">'
    '<span>今日已签到</span>'
    '</body></html>'
)
# 真实已签到页面不再渲染 _csrf（2026-09-01 Actions 实测）：csrf=None 时
# 旧逻辑把它当 Cookie 失效掉进登录兜底，误触邮箱验证风控
PAGE_CHECKED_NO_CSRF = (
    '<html><body><span>今日已签到</span>'
    '<a class="user-name" href="/user/42">烧饼爱好者</a></body></html>'
)
# 模拟 Cloudflare 托管挑战页（站点 2026-08 起对非浏览器客户端全站返回此页）
CF_CHALLENGE_PAGE = (
    '<!DOCTYPE html><html><head><title>Just a moment...</title>'
    '<meta http-equiv="Content-Security-Policy" '
    'content="script-src https://challenges.cloudflare.com">'
    '</head><body><div id="cf-wrapper"></div></body></html>'
)


class FakeResponse:
    """模拟 requests.Response"""

    def __init__(self, status_code=200, text="", json_data=None, url=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.url = url
        # 真实响应总有 headers；默认给空字典，Cloudflare 用例再传 cf-* 头
        self.headers = headers or {}

    def json(self):
        return self._json_data


def fake_get(page_html, status_code=200):
    """构造 GET 请求的 mock"""

    def handler(url, headers=None, cookies=None, timeout=None):
        return FakeResponse(status_code=status_code, text=page_html)

    return mock.patch.object(daily.requests, "get", side_effect=handler)


def fake_get_cf():
    """构造被 Cloudflare 托管挑战拦截的 GET 响应 mock（HTTP 403 + 挑战页）"""

    def handler(url, headers=None, cookies=None, timeout=None):
        return FakeResponse(
            status_code=403,
            text=CF_CHALLENGE_PAGE,
            headers={"Cf-Mitigated": "challenge", "Cf-Ray": "abc123-SJC",
                     "Server": "cloudflare"},
        )

    return mock.patch.object(daily.requests, "get", side_effect=handler)


def fake_post(json_data, status_code=200):
    """构造 POST 请求的 mock"""

    def handler(url, headers=None, cookies=None, data=None, timeout=None):
        return FakeResponse(status_code=status_code, json_data=json_data)

    return mock.patch.object(daily.requests, "post", side_effect=handler)


class FetchCheckinStateTestCase(unittest.TestCase):
    """签到页解析测试"""

    def test_提取csrf且未签到(self):
        with fake_get(PAGE_UNCHECKED):
            csrf, checked_in, _ = daily.fetch_checkin_state("a=1; b=2")
        self.assertEqual(csrf, "abc123csrf")
        self.assertFalse(checked_in)

    def test_识别已签到状态(self):
        with fake_get(PAGE_CHECKED):
            csrf, checked_in, _ = daily.fetch_checkin_state("a=1")
        self.assertEqual(csrf, "abc123csrf")
        self.assertTrue(checked_in)

    def test_cookie失效时无csrf(self):
        with fake_get("<html>请登录</html>"):
            csrf, checked_in, _ = daily.fetch_checkin_state("invalid=1")
        self.assertIsNone(csrf)
        self.assertFalse(checked_in)

    def test_页面错误抛异常(self):
        with fake_get("error", status_code=500):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                daily.fetch_checkin_state("a=1")

    def test_登录页含csrf字段仍判定cookie失效(self):
        """登录页的登录表单同样带 name='_csrf'，不能当作有效签到凭据"""
        page = ('<form><input type="hidden" name="_csrf" value="logincsrf">'
                '<input name="username"><input name="password" type="password">'
                '</form><span>欢迎登录</span>')
        with fake_get(page):
            csrf, checked_in, _ = daily.fetch_checkin_state("bad=1")
        self.assertIsNone(csrf)
        self.assertFalse(checked_in)

    def test_重定向到登录页url时判定cookie失效(self):
        """未登录访问 /daily_checkin 会被 302 到 /login（requests 跟随后 url 为 /login）"""
        page = ('<input type="hidden" name="_csrf" value="logincsrf">'
                '<input name="password" type="password">')
        with mock.patch.object(
            daily.requests, "get",
            side_effect=lambda *a, **k: FakeResponse(
                status_code=200, text=page, url="https://linux.sb/login"
            ),
        ):
            csrf, _, is_login = daily.fetch_checkin_state("a=1")
        self.assertIsNone(csrf)
        self.assertTrue(is_login)

    def test_重定向到登录邮箱确认页时判定未登录(self):
        """站点按客户端环境把 requests 弹到 user_review_login_email（2026-09-01 实测）：
        该页无 password 框、URL 不含 /login，识别不出就会误判 Cookie 失效触发登录兜底"""
        with mock.patch.object(
            daily.requests, "get",
            side_effect=lambda *a, **k: FakeResponse(
                status_code=200, text="<html>已发送验证邮件，请前往邮箱确认</html>",
                url="https://linux.sb/user_review_login_email",
            ),
        ):
            csrf, checked_in, is_login = daily.fetch_checkin_state("a=1")
        self.assertIsNone(csrf)
        self.assertFalse(checked_in)
        self.assertTrue(is_login)

    def test_未登录版签到页判定未登录(self):
        """2026-09-01 取证日志实测的真实形态：站点把 requests 当未登录访客，
        HTTP 200、URL 不变的完整页面，无 csrf/无已签到/无 password 框——
        已登录签到页必渲染 csrf 或已签到文案，两者皆无即未登录版页面"""
        page = ('<html><body><div>每日签到</div>'
                '<div>登录后可以每天签到领取烧饼</div></body></html>')
        with mock.patch.object(
            daily.requests, "get",
            side_effect=lambda *a, **k: FakeResponse(
                status_code=200, text=page, url="https://linux.sb/daily_checkin",
            ),
        ):
            csrf, checked_in, is_login = daily.fetch_checkin_state("a=1")
        self.assertIsNone(csrf)
        self.assertFalse(checked_in)
        self.assertTrue(is_login)


class SignInAccountTestCase(unittest.TestCase):
    """单账号签到流程测试"""

    def test_未签到进入post并成功(self):
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 1, "message": "签到成功"}):
            success, summary, username = daily.sign_in_account("a=1")
        self.assertTrue(success)
        self.assertIn("签到结果: 签到成功（签到成功）", summary)
        self.assertIsNone(username)  # 测试页面无用户名链接

    def test_已签到跳过post(self):
        with fake_get(PAGE_CHECKED) as get_mock, \
             mock.patch.object(daily.requests, "post") as post_mock:
            success, summary, _ = daily.sign_in_account("a=1")
        self.assertTrue(success)
        self.assertIn("今日已签到", summary)
        get_mock.assert_called()
        post_mock.assert_not_called()

    def test_cookie失效给出明确提示(self):
        with fake_get("<html>log in</html>"):
            success, summary, _ = daily.sign_in_account("bad=1")
        self.assertFalse(success)
        self.assertIn("Cookie 已失效", summary)

    def test_页面无csrf且未签到时判未登录不兜底(self):
        """无 csrf 且无已签到文案的页面是未登录版页面（2026-09-01 取证实测）：
        不再用 cookie 的 bbs_csrf 直接发签到 POST（未登录状态必被拒），按未登录
        上报交由 run() 转浏览器复核；浏览器通道另有 fallback_csrf 兜底"""
        page = '<html><body><button>每日签到</button></body></html>'
        with fake_get(page), \
             fake_post({"ok": 1, "message": "签到成功"}) as post_mock:
            success, summary, _ = daily.sign_in_account(
                "bbs_auth=abc; bbs_csrf=cookiecsrf123")
        self.assertFalse(success)
        self.assertIn("未找到 CSRF", summary)
        # 绝不能带着 cookie 里的 bbs_csrf 盲发签到 POST
        post_mock.assert_not_called()

    def test_重复签到时幂等视为成功(self):
        """服务端以 ok:0 + 已打卡/重复签到 返回时，视为当日已签到而非失败"""
        with fake_get(PAGE_UNCHECKED), \
             fake_post({"ok": 0, "message": "今日已打卡，请明天再来"}):
            success, summary, _ = daily.sign_in_account("a=1")
        self.assertTrue(success)
        self.assertIn("无需重复签到", summary)

    def test_post响应额外字段进入摘要(self):
        """ok:1 响应中的积分等字段一并展示（不同站点版本字段名不同）"""
        with fake_get(PAGE_UNCHECKED), \
             fake_post({"ok": 1, "message": "", "bonus": 10, "balance": 888}):
            success, summary, _ = daily.sign_in_account("a=1")
        self.assertTrue(success)
        self.assertIn("bonus: 10", summary)
        self.assertIn("balance: 888", summary)


class ExtractCheckinMetaTestCase(unittest.TestCase):
    """签到页概览信息提取测试"""

    def test_提取积分与连续签到(self):
        html = ('<div>当前积分：1,234</div><div>连续签到 5 天</div>'
                '<span>今日已签到</span>')
        found = dict(daily.extract_checkin_meta(html))
        self.assertEqual(found.get("当前积分"), "1,234")
        self.assertEqual(found.get("连续签到"), "5 天")

    def test_词与冒号间有空格的积分也可提取(self):
        html = '<div>积分 ： 88</div>'
        found = dict(daily.extract_checkin_meta(html))
        self.assertEqual(found.get("积分"), "88")

    def test_无积分信息时返回空(self):
        self.assertEqual(daily.extract_checkin_meta("<html>请先登录</html>"), [])

    def test_噪音词不误匹配积分(self):
        """「积分规则」「获得积分 +10」等不应被当作当前积分"""
        html = ('<div>积分规则：每日签到可获得积分</div>'
                '<div>获得积分 +10</div><div>当前积分：888</div>')
        found = dict(daily.extract_checkin_meta(html))
        self.assertEqual(found.get("当前积分"), "888")
        self.assertNotIn("积分", found)  # 命中了「当前积分」就不应再有重复的「积分」行

    def test_当前积分优先于积分字段(self):
        html = '<div>积分：123</div><div>当前积分：456</div>'
        found = dict(daily.extract_checkin_meta(html))
        self.assertEqual(found.get("当前积分"), "456")
        self.assertNotIn("积分", found)


class CheckinBonusTestCase(unittest.TestCase):
    """“本次签到获得积分”提取测试（POST 响应字段优先，toast 文案兜底）"""

    def test_从bonus字段取积分(self):
        self.assertEqual(daily._checkin_bonus({"ok": 1, "bonus": 76}), 76)

    def test_从points字段取积分(self):
        self.assertEqual(daily._checkin_bonus({"ok": 1, "points": 10}), 10)

    def test_bonus为带符号字符串(self):
        self.assertEqual(daily._checkin_bonus({"bonus": "+10"}), 10)

    def test_响应无积分字段返回None(self):
        self.assertIsNone(daily._checkin_bonus({"ok": 1, "message": "签到成功"}))

    def test_非字典响应返回None(self):
        self.assertIsNone(daily._checkin_bonus(None))
        self.assertIsNone(daily._checkin_bonus("str"))

    def test_从toast文案取积分(self):
        html = '签到成功，连续 2 天，获得 76 积分'
        self.assertEqual(daily._checkin_bonus_from_text(html), "76")

    def test_toast无积分文案返回None(self):
        self.assertIsNone(daily._checkin_bonus_from_text("<html>无签到文案</html>"))

    def test_toast兼容积分加号变体(self):
        html = '签到成功，奖励 8 积分'
        self.assertEqual(daily._checkin_bonus_from_text(html), "8")


class ExtractUsernameTestCase(unittest.TestCase):
    """用户名解析测试"""

    def test_从用户链接提取用户名(self):
        html = ('<a href="/user/42">小明同学</a>'
                '<span>每日签到</span>')
        self.assertEqual(daily.extract_username(html), "小明同学")

    def test_从个人信息卡提取用户名(self):
        """同源论坛程序的 user-name 卡片结构（登录态各页面通用）"""
        html = ('<div class="user-card">'
                '<a class="user-name" href="/user/42">烧饼爱好者</a>'
                '</div><div>当前积分：888</div>')
        self.assertEqual(daily.extract_username(html), "烧饼爱好者")

    def test_无用户信息时返回None(self):
        self.assertIsNone(daily.extract_username("<html>请先登录</html>"))

    def test_卡片结构优先于链接(self):
        html = ('<a class="user-name" href="/user/42">卡片用户名</a>'
                '<a href="/user/41">别人</a>')
        self.assertEqual(daily.extract_username(html), "卡片用户名")


class ExtractUsernameRunTestCase(unittest.TestCase):
    """通知中账号标识测试：使用用户名，绝不暴露 cookie 内容"""

    def setUp(self):
        # 与 RunTestCase 一致：屏蔽 run() 签到的 SITE_GAP 随机延迟
        self.sleep_mock = mock.patch.object(daily.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        import os

        os.environ.pop("LINUXSB_COOKIE", None)

    def test_通知使用用户名而非cookie键名(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc; bbs_csrf=csrf123"
        page = ('<a href="/user/42">小明同学</a>'
                '<div>当前积分：888</div>'
                '<input type="hidden" name="_csrf" value="abc">')
        with fake_get(page), fake_post({"ok": 1, "message": "好"}), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        content = send_mock.call_args.args[1]
        self.assertIn("小明同学", content)
        # cookie 键名 bbs_auth / bbs_csrf 不进入通知
        self.assertNotIn("bbs_auth", content)
        self.assertNotIn("bbs_csrf", content)

    def test_服务端拒绝时返回失败不抛异常(self):
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 0, "message": "请求已过期"}):
            success, summary, _ = daily.sign_in_account("a=1")
        self.assertFalse(success)
        self.assertIn("请求已过期", summary)
        self.assertIn("更新 LINUXSB_COOKIE", summary)


class AccountLoginTestCase(unittest.TestCase):
    """账号密码兜底登录相关测试"""

    def tearDown(self):
        import os

        for name in ("LINUXSB_ACCOUNT", "LINUXSB_COOKIE"):
            os.environ.pop(name, None)

    def test_解析合法凭据(self):
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "xiao ming", "password": "p@ss:word"}'
        self.assertEqual(daily.load_account_creds(),
                         {"username": "xiao ming", "password": "p@ss:word"})

    def test_未配置或非法JSON返回None(self):
        os.environ.pop("LINUXSB_ACCOUNT", None)
        self.assertIsNone(daily.load_account_creds())
        os.environ["LINUXSB_ACCOUNT"] = "not-json"
        self.assertIsNone(daily.load_account_creds())
        os.environ["LINUXSB_ACCOUNT"] = '{"username": ""}'
        self.assertIsNone(daily.load_account_creds())

    def test_算术题四则运算(self):
        cases = {
            "9 × 4 = ?": "36",
            "7 + 3 = ?": "10",
            "12 - 5 = ?": "7",
            "8 ÷ 2 = ?": "4",
            "3 * 4 = ?": "12",
        }
        for question, expected in cases.items():
            self.assertEqual(daily.solve_captcha_question(question), expected)

    def test_算术题无法解析抛异常(self):
        with self.assertRaises(ValueError):
            daily.solve_captcha_question("请输入验证码")


class RunLoginFallbackTestCase(unittest.TestCase):
    """run() 内 cookie 失效降级登录流程测试（账号密码登录后就地签到）"""

    def setUp(self):
        # 屏蔽 run() 签到的 SITE_GAP 随机延迟
        self.sleep_mock = mock.patch.object(daily.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        import os

        for name in ("LINUXSB_COOKIE", "LINUXSB_ACCOUNT"):
            os.environ.pop(name, None)

    def test_cookie失效时浏览器登录签到(self):
        os.environ["LINUXSB_COOKIE"] = "a=1"
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        # 探测被弹回登录页（requests 环境未取得登录态）→ 浏览器 Cookie 复核
        # 仍无登录态（真失效）→ 账号密码浏览器登录兜底，登录成功就地签到。
        with fake_get("<html>请登录</html>"), \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               side_effect=daily.CookieExpired("登录态未生效")), \
             mock.patch.object(daily, "browser_sign_in_with_retry",
                               return_value=(True, "签到结果: 签到成功（浏览器）", "小明")) as sign_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        sign_mock.assert_called_once()
        self.assertEqual(sign_mock.call_args.args[0],
                         {"username": "u", "password": "p"})
        content = send_mock.call_args.args[1]
        self.assertIn("浏览器", content)
        self.assertIn("小明", content)

    def test_仅配置账号密码时浏览器登录签到(self):
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        with mock.patch.object(daily, "browser_sign_in_with_retry",
                               return_value=(True, "签到结果: 签到成功", None)) as sign_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        sign_mock.assert_called_once()
        self.assertIn("签到成功", send_mock.call_args.args[1])

    def test_cookie失效且无凭据时明确报错(self):
        os.environ["LINUXSB_COOKIE"] = "a=1"
        with fake_get("<html>请登录</html>"), \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               side_effect=daily.CookieExpired("登录态未生效")), \
             mock.patch.object(daily, "browser_sign_in_with_retry") as sign_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 1)
        sign_mock.assert_not_called()
        self.assertIn("Cookie 已失效", send_mock.call_args.args[1])

    def test_cookie有效时走requests签到不启动浏览器(self):
        os.environ["LINUXSB_COOKIE"] = "a=1"
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 1, "message": "好"}), \
             mock.patch.object(daily, "browser_sign_in_with_retry") as sign_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        sign_mock.assert_not_called()
        self.assertIn("签到成功", send_mock.call_args.args[1])

    def test_无cookie无凭据时静默跳过(self):
        with mock.patch.object(daily.notify, "send") as send_mock, \
             mock.patch.object(daily, "browser_sign_in_with_retry") as sign_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        send_mock.assert_not_called()
        sign_mock.assert_not_called()


class CloudflareDetectTestCase(unittest.TestCase):
    """Cloudflare 挑战识别与 cookie 注入过滤测试"""

    def test_响应头cf_mitigated判定为挑战(self):
        resp = FakeResponse(status_code=403, text="",
                            headers={"Cf-Mitigated": "challenge"})
        self.assertTrue(daily.is_cf_challenge_response(resp))

    def test_响应头大小写不敏感(self):
        resp = FakeResponse(status_code=403, text="",
                            headers={"cf-mitigated": "CHALLENGE"})
        self.assertTrue(daily.is_cf_challenge_response(resp))

    def test_挑战页正文特征判定为挑战(self):
        """没有 cf-mitigated 头时，靠 Just a moment 标题等正文特征识别"""
        resp = FakeResponse(status_code=403, text=CF_CHALLENGE_PAGE)
        self.assertTrue(daily.is_cf_challenge_response(resp))

    def test_普通错误页不误判为挑战(self):
        resp = FakeResponse(status_code=500, text="<html>Internal Error</html>")
        self.assertFalse(daily.is_cf_challenge_response(resp))

    def test_无headers属性的响应也能判定(self):
        """兼容不带 headers 的响应对象，只看正文特征，不应抛 AttributeError"""

        class Bare:
            status_code = 403
            text = CF_CHALLENGE_PAGE

        self.assertTrue(daily.is_cf_challenge_response(Bare()))

    def test_跳过与IP和UA绑定的cookie(self):
        for name in ("cf_clearance", "__cf_bm", "_ga", "_ga_ABC123", "_gid",
                     "CF_CLEARANCE"):
            self.assertTrue(daily.should_skip_cookie(name), name)

    def test_登录态cookie不跳过(self):
        for name in ("bbs_auth", "bbs_csrf", "__daily_checkin_stats"):
            self.assertFalse(daily.should_skip_cookie(name), name)


class FetchCheckinStateCloudflareTestCase(unittest.TestCase):
    """requests 通道被 Cloudflare 挑战时的异常分型测试"""

    def test_挑战拦截抛CloudflareChallenged(self):
        """403 + Cf-Mitigated: challenge 必须抛专用异常，供 run() 切浏览器通道"""
        with fake_get_cf():
            with self.assertRaises(daily.CloudflareChallenged) as ctx:
                daily.fetch_checkin_state("bbs_auth=abc")
        # 异常信息带 cf-mitigated / cf-ray，便于从 Actions 日志直接确认是挑战
        self.assertIn("Cloudflare", str(ctx.exception))
        self.assertIn("challenge", str(ctx.exception))
        self.assertIn("abc123-SJC", str(ctx.exception))

    def test_普通403不抛CloudflareChallenged(self):
        """非挑战的 403（如 IP 封禁）仍按普通失败处理，不该白启动浏览器"""
        with fake_get("<html>Forbidden</html>", status_code=403):
            with self.assertRaises(RuntimeError) as ctx:
                daily.fetch_checkin_state("bbs_auth=abc")
        self.assertNotIsInstance(ctx.exception, daily.CloudflareChallenged)
        self.assertIn("HTTP 403", str(ctx.exception))


class BrowserRetryTestCase(unittest.TestCase):
    """浏览器动作重试策略测试"""

    def setUp(self):
        # 屏蔽重试间隔的 sleep，避免单测真实等待
        mock.patch.object(daily.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_首次成功不重试(self):
        calls = []

        def func(arg):
            calls.append(arg)
            return True, "ok", None

        self.assertEqual(daily._browser_with_retry("测试", func, "x"),
                         (True, "ok", None))
        self.assertEqual(calls, ["x"])

    def test_偶发失败后重试成功(self):
        calls = []

        def func(arg):
            calls.append(arg)
            if len(calls) == 1:
                raise RuntimeError("导航卡死")
            return True, "ok", None

        self.assertEqual(daily._browser_with_retry("测试", func, "x"),
                         (True, "ok", None))
        self.assertEqual(len(calls), 2)

    def test_cookie失效不重试直接上抛(self):
        """Cookie 失效重开浏览器也不会变有效，必须立刻上抛给凭据兜底"""
        calls = []

        def func(arg):
            calls.append(arg)
            raise daily.CookieExpired("登录态未生效")

        with self.assertRaises(daily.CookieExpired):
            daily._browser_with_retry("测试", func, "x")
        self.assertEqual(len(calls), 1)

    def test_连续失败抛出汇总异常(self):
        def func(arg):
            raise RuntimeError("导航卡死")

        with self.assertRaisesRegex(RuntimeError, "连续 2 次失败"):
            daily._browser_with_retry("测试", func, "x", max_attempts=2)


class RunCloudflareFallbackTestCase(unittest.TestCase):
    """run() 在 Cloudflare 挑战下改走浏览器通道的流程测试"""

    def setUp(self):
        # 屏蔽 run() 签到前的 SITE_GAP 随机延迟
        mock.patch.object(daily.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        for name in ("LINUXSB_COOKIE", "LINUXSB_ACCOUNT", "LINUXSB_FORCE_BROWSER"):
            os.environ.pop(name, None)

    def test_挑战时改用浏览器注入cookie签到(self):
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc; bbs_csrf=csrf1"
        with fake_get_cf(), \
             mock.patch.object(daily.requests, "post") as post_mock, \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               return_value=(True, "签到结果: 签到成功（浏览器）",
                                             "小明")) as cookie_mock, \
             mock.patch.object(daily, "browser_sign_in_with_retry") as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        # 走浏览器 Cookie 通道，且原样传入该账号的 cookie
        cookie_mock.assert_called_once_with("bbs_auth=abc; bbs_csrf=csrf1")
        # 挑战下 requests 通道整条不可用，绝不能再发签到 POST
        post_mock.assert_not_called()
        # 未配置凭据也不该触碰账号密码通道
        login_mock.assert_not_called()
        self.assertIn("小明", send_mock.call_args.args[1])

    def test_挑战且cookie失效时回落账号密码登录(self):
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=expired"
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        with fake_get_cf(), \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               side_effect=daily.CookieExpired("登录态未生效")), \
             mock.patch.object(daily, "browser_sign_in_with_retry",
                               return_value=(True, "签到结果: 签到成功", "小明")
                               ) as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        login_mock.assert_called_once()
        self.assertEqual(login_mock.call_args.args[0],
                         {"username": "u", "password": "p"})
        self.assertIn("签到成功", send_mock.call_args.args[1])

    def test_挑战且cookie失效且无凭据时明确报错(self):
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=expired"
        with fake_get_cf(), \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               side_effect=daily.CookieExpired("登录态未生效")), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 1)
        content = send_mock.call_args.args[1]
        self.assertIn("Cookie 已失效", content)
        self.assertIn("更新 LINUXSB_COOKIE", content)

    def test_挑战下浏览器通道失败时如实报告(self):
        """浏览器也过不去时按失败处理，退出码 1，摘要保留原因"""
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc"
        with fake_get_cf(), \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               side_effect=RuntimeError("浏览器 Cookie 签到连续 2 次失败")), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 1)
        self.assertIn("连续 2 次失败", send_mock.call_args.args[1])

    def test_探测通过后中途开盾也走浏览器(self):
        """站点在探测之后才开挑战：requests 签到抛挑战异常，同样转浏览器通道"""
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc"
        calls = []

        def get_handler(url, headers=None, cookies=None, timeout=None):
            calls.append(url)
            if len(calls) == 1:  # 第一次探测正常放行
                return FakeResponse(text=PAGE_UNCHECKED)
            return FakeResponse(status_code=403, text=CF_CHALLENGE_PAGE,
                                headers={"Cf-Mitigated": "challenge"})

        with mock.patch.object(daily.requests, "get", side_effect=get_handler), \
             mock.patch.object(daily.requests, "post") as post_mock, \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               return_value=(True, "签到结果: 签到成功（浏览器）", None)
                               ) as cookie_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        cookie_mock.assert_called_once()
        post_mock.assert_not_called()
        self.assertIn("浏览器", send_mock.call_args.args[1])

    def test_多账号中一个被挑战不影响其余账号(self):
        """挑战是全站规则，但通道判定按账号独立，单账号浏览器失败不拖垮其他账号"""
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=a&bbs_auth=b"

        def get_handler(url, headers=None, cookies=None, timeout=None):
            # 账号 a 被挑战（走浏览器），账号 b 正常（走 requests）
            if cookies and cookies.get("bbs_auth") == "a":
                return FakeResponse(status_code=403, text=CF_CHALLENGE_PAGE,
                                    headers={"Cf-Mitigated": "challenge"})
            return FakeResponse(text=PAGE_UNCHECKED)

        with mock.patch.object(daily.requests, "get", side_effect=get_handler), \
             fake_post({"ok": 1, "message": "签到成功"}) as post_mock, \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               side_effect=RuntimeError("浏览器起不来")), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 1)
        # 账号 b 正常完成 requests 签到
        self.assertEqual(post_mock.call_count, 1)
        content = send_mock.call_args.args[1]
        self.assertIn("账号 1", content)
        self.assertIn("账号 2", content)
        self.assertIn("浏览器起不来", content)

    def test_强制开关跳过requests探测直接走浏览器(self):
        """LINUXSB_FORCE_BROWSER=1：站点未开盾时也直接走浏览器通道，一个请求都不发"""
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc"
        os.environ["LINUXSB_FORCE_BROWSER"] = "1"
        with mock.patch.object(daily.requests, "get") as get_mock, \
             mock.patch.object(daily.requests, "post") as post_mock, \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               return_value=(True, "签到结果: 签到成功（浏览器）",
                                             "小明")) as cookie_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        cookie_mock.assert_called_once_with("bbs_auth=abc")
        # 强制模式下连探测都不该发出
        get_mock.assert_not_called()
        post_mock.assert_not_called()
        self.assertIn("小明", send_mock.call_args.args[1])

    def test_强制开关不把无cookie账号送进cookie通道(self):
        """只配了凭据时，强制开关必须让账号走账号密码通道，而非注入 None"""
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        os.environ["LINUXSB_FORCE_BROWSER"] = "1"
        with mock.patch.object(daily, "browser_cookie_sign_in_with_retry"
                               ) as cookie_mock, \
             mock.patch.object(daily, "browser_sign_in_with_retry",
                               return_value=(True, "签到结果: 签到成功", "小明")
                               ) as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        cookie_mock.assert_not_called()
        login_mock.assert_called_once()
        self.assertIn("小明", send_mock.call_args.args[1])


# requests 探测被弹回登录页时页面特征：fetch_checkin_state 判 is_login=True
LOGIN_REDIRECT_PAGE = (
    '<html><body><form><input name="username" type="text">'
    '<input name="password" type="password"></form></body></html>'
)


class RunRequestsLoginFallbackTestCase(unittest.TestCase):
    """run() 在 requests 判未登录（302 到登录页）时先浏览器复核 Cookie 的流程测试

    2026-09-01 实测：站点按客户端环境校验会话——同一份 Cookie，requests 探测
    被 302 到 /login，浏览器注入后登录态有效。requests 判未登录不能直接当
    Cookie 失效转账号密码登录（登录兜底可能撞邮箱验证风控），必须先复核。
    """

    def setUp(self):
        # 屏蔽 run() 签到前的 SITE_GAP 随机延迟
        mock.patch.object(daily.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        for name in ("LINUXSB_COOKIE", "LINUXSB_ACCOUNT", "LINUXSB_FORCE_BROWSER"):
            os.environ.pop(name, None)

    def test_requests判未登录时先浏览器复核cookie(self):
        """复核成功（登录态对浏览器环境有效）直接就地签到，不动用账号密码登录"""
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc"
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        with fake_get(LOGIN_REDIRECT_PAGE), \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               return_value=(True, "签到结果: 签到成功（浏览器）",
                                             "小明")) as cookie_mock, \
             mock.patch.object(daily, "browser_sign_in_with_retry") as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        # 复核通道收到原样 cookie
        cookie_mock.assert_called_once_with("bbs_auth=abc")
        # 复核已成功就不该再动用账号密码登录
        login_mock.assert_not_called()
        self.assertIn("小明", send_mock.call_args.args[1])

    def test_浏览器复核仍失效时回落账号密码登录(self):
        """浏览器内仍无登录态才是真失效：转账号密码兜底登录"""
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=expired"
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        with fake_get(LOGIN_REDIRECT_PAGE), \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               side_effect=daily.CookieExpired("登录态未生效")
                               ) as cookie_mock, \
             mock.patch.object(daily, "browser_sign_in_with_retry",
                               return_value=(True, "签到结果: 签到成功", "小明")
                               ) as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        cookie_mock.assert_called_once()
        login_mock.assert_called_once_with({"username": "u", "password": "p"})
        self.assertIn("签到成功", send_mock.call_args.args[1])

    def test_复核失效且无凭据时报cookie失效(self):
        """无凭据兜底时如实报失效并指引更新 Cookie"""
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=expired"
        with fake_get(LOGIN_REDIRECT_PAGE), \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               side_effect=daily.CookieExpired("登录态未生效")), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 1)
        content = send_mock.call_args.args[1]
        self.assertIn("Cookie 已失效", content)
        self.assertIn("更新 LINUXSB_COOKIE", content)

    def test_探测到已签到时直接成功不碰浏览器(self):
        """已签到页不渲染 _csrf（csrf=None）：按成功收尾，不能误判失效触发登录兜底"""
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc"
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        with fake_get(PAGE_CHECKED_NO_CSRF), \
             mock.patch.object(daily.requests, "post") as post_mock, \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry"
                               ) as cookie_mock, \
             mock.patch.object(daily, "browser_sign_in_with_retry") as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        # 已签到既不发签到 POST，也不开任何浏览器
        post_mock.assert_not_called()
        cookie_mock.assert_not_called()
        login_mock.assert_not_called()
        content = send_mock.call_args.args[1]
        self.assertIn("今日已签到", content)

    def test_被弹到邮箱验证页时浏览器复核(self):
        """2026-09-01 Actions 真实路径：requests 探测 302 到 user_review_login_email，
        验证页无 password 框与 csrf——必须识别为未登录走浏览器复核，而非登录兜底"""
        def get_handler(url, headers=None, cookies=None, timeout=None):
            return FakeResponse(text="<html>已发送验证邮件，请前往邮箱确认</html>",
                                url="https://linux.sb/user_review_login_email")

        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc"
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        with mock.patch.object(daily.requests, "get", side_effect=get_handler), \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry",
                               return_value=(True, "签到结果: 签到成功（浏览器）",
                                             "小明")) as cookie_mock, \
             mock.patch.object(daily, "browser_sign_in_with_retry") as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        # 浏览器复核成功后不碰账号密码登录（会撞邮箱验证风控）
        cookie_mock.assert_called_once_with("bbs_auth=abc")
        login_mock.assert_not_called()
        self.assertIn("小明", send_mock.call_args.args[1])

    def test_未设置强制开关时仍优先requests快通道(self):
        """开关默认关闭：站点未开盾时保持 requests 通道，不无谓启动浏览器"""
        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc"
        with fake_get(PAGE_UNCHECKED), \
             fake_post({"ok": 1, "message": "签到成功"}) as post_mock, \
             mock.patch.object(daily, "browser_cookie_sign_in_with_retry"
                               ) as cookie_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        self.assertEqual(post_mock.call_count, 1)
        cookie_mock.assert_not_called()
        self.assertIn("签到成功", send_mock.call_args.args[1])


class FakeDriver:
    """
    模拟 selenium WebDriver 的最小子集，用于在不启动真实浏览器的前提下测试
    浏览器通道的判断逻辑（重定向识别、CSRF 取值、签到响应分类）。

    pages: {URL 关键字: HTML}，按 get/refresh 的目标切换 page_source；
    landing: get 之后实际停留的 URL（模拟 302 到 /login）。
    """

    def __init__(self, page, cookies=None, script_result=None, landing=None,
                 title="每日签到 - 烧饼社区", captcha_question=None,
                 login_csrf=None, after_login_page=None, after_login_url=None):
        self.page = page
        self._cookies = cookies or []
        self.script_result = script_result
        self.landing = landing
        self.title = title
        self.current_url = ""
        self.dom_queries = []      # wait_dom_ready 的调用记录
        self.async_scripts = []    # 签到 fetch 的调用记录
        self.refreshed = 0
        # 登录表单 JS 填写流程（execute_script 三类脚本）的配置与记录
        self.captcha_question = captcha_question  # 读题面脚本的返回值
        self.login_csrf = login_csrf              # 填表脚本返回的 _csrf
        self.after_login_page = after_login_page  # 提交后切换到的页面（模拟登录成功）
        self.after_login_url = after_login_url    # 提交后跳转的 URL（如邮箱验证页）
        self.fill_calls = []                      # (username, password, answer) 记录
        self.submitted = False
        self.added_cookies = []                   # add_cookie 的调用记录
        self.get_calls = []                       # get() 的目标 URL 记录

    def get(self, url):
        self.get_calls.append(url)
        self.current_url = self.landing or url

    def add_cookie(self, cookie):
        # 模拟 chromedriver：不带 domain 时按当前页面 host 落域（这里只记录调用）
        self.added_cookies.append(cookie)

    def refresh(self):
        self.refreshed += 1

    def quit(self):
        pass

    @property
    def page_source(self):
        return self.page

    def set_script_timeout(self, seconds):
        pass

    def get_cookies(self):
        return self._cookies

    def execute_script(self, script, *args):
        # 登录流程的取值/填表/提交脚本按内容特征分派；其余调用视为
        # wait_dom_ready 的 querySelector 探测（按选择器是否命中 HTML 判定）
        if "native-captcha-question" in script and "textContent" in script:
            return self.captcha_question or ""
        if "dispatchEvent" in script:
            self.fill_calls.append(args)
            return self.login_csrf
        if "requestSubmit" in script:
            self.submitted = True
            if self.after_login_page is not None:
                self.page = self.after_login_page
            if self.after_login_url is not None:
                self.current_url = self.after_login_url
            return True
        selector = args[0] if args else ""
        self.dom_queries.append(selector)
        return self._selector_hit(selector)

    def _selector_hit(self, selector):
        """
        模拟 querySelector 对页面的命中判断，支持两种选择器形式：
        .class（类名出现在 HTML）与 [name=xxx]（对应属性存在于 HTML）。
        """
        for part in selector.split(","):
            part = part.strip()
            name_match = re.fullmatch(r"\[name=([\w-]+)\]", part)
            if name_match:
                name = name_match.group(1)
                if f'name="{name}"' in self.page or f"name={name}" in self.page:
                    return True
            elif part.startswith(".") and part.lstrip(".") in self.page:
                return True
        return False

    def execute_async_script(self, script, *args):
        self.async_scripts.append(args)
        return self.script_result


# 登录态签到页（含站点真实类名 daily-checkin-wrap，未签到）
BROWSER_PAGE_UNCHECKED = (
    '<html><head><title>每日签到</title></head><body>'
    '<div class="daily-checkin-wrap"><button>每日签到</button></div>'
    '<a class="user-name" href="/user/42">烧饼爱好者</a>'
    '<div>当前积分：888</div></body></html>'
)
# 登录态签到页（已签到）
BROWSER_PAGE_CHECKED = BROWSER_PAGE_UNCHECKED.replace(
    '<button>每日签到</button>', '<span>今日已签到</span>'
)
# 登录页（结构与站点 v8.7.5 快照一致：表单含 csrf/凭据/验证码/PoW 与蜜罐字段）
LOGIN_PAGE = (
    '<html><head><title>登录 - 烧饼社区</title></head><body>'
    '<form method="post" data-slot="login.form_extra">'
    '<input type="hidden" name="_csrf" value="logincsrf">'
    '<input name="username" type="text" value="">'
    '<input name="password" type="password">'
    '<div class="user-review-native-captcha" data-native-captcha="">'
    '<div class="native-captcha-question">9 × 7 = ?</div>'
    '<input class="native-captcha-answer" name="native_captcha_answer" type="text">'
    '<input type="hidden" name="native_captcha_pow" value="97d">'
    '<input type="text" name="native_captcha_company" tabindex="-1">'
    '</div><button>登录</button></form></body></html>'
)


class CheckinInBrowserTestCase(unittest.TestCase):
    """浏览器通道内签到逻辑测试（Cookie 注入与账号密码登录两条通道的汇合点）"""

    def _stage(self):
        return daily._Stage("测试")

    def test_重定向到登录页立即判定cookie失效(self):
        """未登录时 /daily_checkin 会 302 到 /login：必须先判 URL，不白等 DOM 超时"""
        driver = FakeDriver('<html><title>登录</title><body>登录</body></html>',
                            landing="https://linux.sb/login")
        with self.assertRaises(daily.CookieExpired):
            daily._checkin_in_browser(driver, self._stage())
        # 没有进入等待 DOM 的环节（否则要空等满超时且异常类型退化）
        self.assertEqual(driver.dom_queries, [])

    def test_已签到时不再发签到请求(self):
        driver = FakeDriver(BROWSER_PAGE_CHECKED)
        lines, username = daily._checkin_in_browser(driver, self._stage())
        self.assertIn("今日已签到", lines[0])
        self.assertEqual(driver.async_scripts, [])
        self.assertEqual(username, "烧饼爱好者")

    def test_用浏览器现场的bbs_csrf签到并提取概览(self):
        driver = FakeDriver(
            BROWSER_PAGE_UNCHECKED,
            cookies=[{"name": "bbs_auth", "value": "abc"},
                     {"name": "bbs_csrf", "value": "livecsrf"}],
            script_result={"ok": 1, "message": "签到成功", "bonus": 76},
        )
        lines, username = daily._checkin_in_browser(driver, self._stage())
        # 提交的 _csrf 取自浏览器当前 cookie，保证与请求携带的 cookie 一致
        self.assertEqual(driver.async_scripts[0][0], "livecsrf")
        self.assertIn("签到成功", lines[0])
        self.assertIn("本次签到获得: 76 积分", lines)
        self.assertIn("当前积分: 888", lines)
        self.assertEqual(username, "烧饼爱好者")
        self.assertEqual(driver.refreshed, 1)

    def test_bbs_csrf被服务端删除时回退备用令牌(self):
        driver = FakeDriver(
            BROWSER_PAGE_UNCHECKED,
            cookies=[{"name": "bbs_csrf", "value": "deleted"}],
            script_result={"ok": 1, "message": ""},
        )
        daily._checkin_in_browser(driver, self._stage(), fallback_csrf="backup")
        self.assertEqual(driver.async_scripts[0][0], "backup")

    def test_取不到csrf且无备用令牌时判定cookie失效(self):
        driver = FakeDriver(BROWSER_PAGE_UNCHECKED, cookies=[],
                            script_result={"ok": 1})
        with self.assertRaises(daily.CookieExpired):
            daily._checkin_in_browser(driver, self._stage())
        self.assertEqual(driver.async_scripts, [])

    def test_服务端拒绝抛CheckinRejected(self):
        driver = FakeDriver(
            BROWSER_PAGE_UNCHECKED,
            cookies=[{"name": "bbs_csrf", "value": "live"}],
            script_result={"ok": 0, "message": "请求已过期"},
        )
        with self.assertRaisesRegex(daily.CheckinRejected, "请求已过期"):
            daily._checkin_in_browser(driver, self._stage())

    def test_重复签到按已签到处理(self):
        driver = FakeDriver(
            BROWSER_PAGE_UNCHECKED,
            cookies=[{"name": "bbs_csrf", "value": "live"}],
            script_result={"ok": 0, "message": "今日已打卡，请明天再来"},
        )
        lines, _ = daily._checkin_in_browser(driver, self._stage())
        self.assertIn("今日已签到", lines[0])


    def test_页面形态对不上仍完成签到(self):
        """模板改版导致就绪选择器落空时只警告不失败——签到由 fetch 完成，不依赖元素"""
        driver = FakeDriver(
            '<html><body><div class="brand-new-layout">签到</div></body></html>',
            cookies=[{"name": "bbs_csrf", "value": "live"}],
            script_result={"ok": 1, "message": "签到成功"},
        )
        # 把就绪等待上限压到 0.2 秒，避免单测真等满 20 秒
        with mock.patch.object(daily, "CHECKIN_PAGE_READY_TIMEOUT", 0.2):
            lines, _ = daily._checkin_in_browser(driver, self._stage())
        self.assertIn("签到成功", lines[0])
        self.assertEqual(driver.async_scripts[0][0], "live")

    def test_登录表单缺失时硬失败(self):
        """required=True 的元素（登录表单）拿不到必须抛异常，否则后续操作无从下手"""
        driver = FakeDriver('<html><body>空页面</body></html>')
        with mock.patch.object(daily.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, r"\[name=username\]"):
                daily.wait_dom_ready(driver, "[name=username]", timeout=1)


class BrowserSignInJsFormTestCase(unittest.TestCase):
    """账号密码浏览器登录测试：表单读写全部经 execute_script（绕开 UCD 元素 API）"""

    CREDS = {"username": "u", "password": "p"}

    def _run_login(self, **driver_kwargs):
        driver = FakeDriver(LOGIN_PAGE, **driver_kwargs)
        # 屏蔽提交前模拟真人节奏的 8-16 秒随机等待（避免单测真实 sleep）
        with mock.patch.object(daily, "create_driver", return_value=driver), \
             mock.patch.object(daily.time, "sleep") as sleep_mock:
            result = daily.browser_sign_in(dict(self.CREDS))
        return driver, result, sleep_mock

    def test_js填写凭据与验证码答案并提交(self):
        """题面解析结果随凭据一起经 arguments 传入填表脚本，提交用 requestSubmit"""
        driver, (ok, summary, username), sleep_mock = self._run_login(
            captcha_question="9 × 7 = ?", login_csrf="logincsrf",
            after_login_page=BROWSER_PAGE_CHECKED,
        )
        self.assertTrue(ok)
        # 填表参数 = (username, password, 验证码答案)，蜜罐字段不在其中
        self.assertEqual(driver.fill_calls, [("u", "p", "63")])
        self.assertTrue(driver.submitted)
        # 提交前必须先按真人节奏等待（服务端有「提交过快」风控）
        self.assertTrue(any(call.args[0] >= 8 for call in sleep_mock.call_args_list))
        self.assertIn("今日已签到", summary)
        self.assertEqual(username, "烧饼爱好者")

    def test_题面取不到时按解析失败上报(self):
        """题面为空（模板改版）时 solve_captcha_question 抛错，失败信息带阶段与 URL"""
        with self.assertRaisesRegex(RuntimeError, r"填写登录表单.*linux\.sb/login"):
            self._run_login(captcha_question="", login_csrf="logincsrf")
        with self.assertRaisesRegex(RuntimeError, r"填写登录表单"):
            self._run_login(captcha_question="请输入验证码", login_csrf="logincsrf")

    def test_填表取不到csrf时按页面改版上报(self):
        """_csrf 为空说明登录表单结构已变，必须失败而不是带着空令牌继续"""
        with self.assertRaisesRegex(RuntimeError, "结构校验失败"):
            self._run_login(captcha_question="9 × 7 = ?", login_csrf="")

    def test_提交后停留登录页时超时失败(self):
        """登录失败（如密码错误）会停在 /login：等待超时后异常带阶段与 URL"""
        driver = FakeDriver(LOGIN_PAGE, captcha_question="9 × 7 = ?",
                            login_csrf="logincsrf")
        # after_login_page 不设置：提交后页面仍是登录页（password 字段一直在）

        class InstantTimeoutWait:
            """跳过真实轮询，直接抛与 selenium 一致的 TimeoutException"""

            def __init__(self, driver, timeout):
                pass

            def until(self, method, message=""):
                from selenium.common.exceptions import TimeoutException
                raise TimeoutException()

        with mock.patch.object(daily, "create_driver", return_value=driver), \
             mock.patch.object(daily.time, "sleep"), \
             mock.patch("selenium.webdriver.support.ui.WebDriverWait",
                        InstantTimeoutWait):
            with self.assertRaisesRegex(RuntimeError,
                                        r"等待提交（模拟真人输入节奏）.*linux\.sb/login"):
                daily.browser_sign_in(dict(self.CREDS))
        self.assertTrue(driver.submitted)

    def test_提交后落入邮箱验证页时上报需人工介入(self):
        """站点风控要求邮箱二次确认（user_review_login_email）：抛 LoginVerificationRequired 而非误判登录成功"""
        # 验证页无 password 框（密码等待会通过），但登录态并未建立
        review_page = ('<html><head><title>登录确认</title></head><body>'
                       '<div>已发送验证邮件，请前往邮箱确认</div></body></html>')
        driver = FakeDriver(LOGIN_PAGE, captcha_question="9 × 7 = ?",
                            login_csrf="logincsrf", after_login_page=review_page,
                            after_login_url="https://linux.sb/user_review_login_email")
        with mock.patch.object(daily, "create_driver", return_value=driver), \
             mock.patch.object(daily.time, "sleep"):
            with self.assertRaisesRegex(daily.LoginVerificationRequired,
                                        "user_review_login_email.*LINUXSB_COOKIE"):
                daily.browser_sign_in(dict(self.CREDS))
        self.assertTrue(driver.submitted)

    def test_邮箱验证要求不触发浏览器重试(self):
        """LoginVerificationRequired 属人工介入事项：_browser_with_retry 不重开浏览器直接上抛"""
        calls = []

        def always_rejected(_):
            calls.append(1)
            raise daily.LoginVerificationRequired("需邮箱验证")

        with mock.patch.object(daily.time, "sleep") as sleep_mock:
            with self.assertRaises(daily.LoginVerificationRequired):
                daily._browser_with_retry("登录", always_rejected, None,
                                          max_attempts=3)
        # 只执行一次（CookieExpired 同款处理），既不重试也不做重试间隔等待
        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()


class BrowserCookieInjectTestCase(unittest.TestCase):
    """浏览器 Cookie 注入通道测试（2026-08-30 invalid cookie domain 修复的回归）"""

    def test_注入不带domain并跳过IP绑定项(self):
        """add_cookie 不传 domain（Chrome 151 chromedriver 拒绝显式 domain），cf_clearance 等照旧跳过"""
        driver = FakeDriver(BROWSER_PAGE_CHECKED,
                            cookies=[{"name": "bbs_auth", "value": "x"}])
        cookie = ("bbs_auth=abc; bbs_csrf=csrf1; cf_clearance=old; _ga_x=1; "
                  "__recent_forums=2")
        with mock.patch.object(daily, "create_driver", return_value=driver):
            ok, summary, username = daily.browser_sign_in_with_cookie(cookie)
        self.assertTrue(ok)
        # 注入的 cookie 全部不带 domain 键（由 chromedriver 按当前页面落域）
        for item in driver.added_cookies:
            self.assertNotIn("domain", item)
        injected_names = [c["name"] for c in driver.added_cookies]
        self.assertEqual(injected_names, ["bbs_auth", "bbs_csrf", "__recent_forums"])
        self.assertIn("今日已签到", summary)

    def test_当前页不在目标域时先回首页再注入(self):
        """浏览器停在非 linux.sb 页面（如空白页）时，先重新打开首页再注入，避免 cookie 落错域"""
        driver = FakeDriver(BROWSER_PAGE_CHECKED, landing="about:blank",
                            cookies=[{"name": "bbs_auth", "value": "x"}])
        with mock.patch.object(daily, "create_driver", return_value=driver):
            ok, _, _ = daily.browser_sign_in_with_cookie("bbs_auth=abc")
        self.assertTrue(ok)
        # 第一次 get 首页 + 发现不在目标域后重新 get 首页
        self.assertEqual(driver.get_calls.count(daily.BASE_URL), 2)
        self.assertTrue(driver.added_cookies)

    def test_无有效cookie可注入时判定失效(self):
        """cookie 字符串全是跳过项时注入 0 个，抛 CookieExpired 交给上层转账号密码兜底"""
        driver = FakeDriver(BROWSER_PAGE_CHECKED)
        with mock.patch.object(daily, "create_driver", return_value=driver):
            with self.assertRaises(daily.CookieExpired):
                daily.browser_sign_in_with_cookie("cf_clearance=old; _ga=1")
        self.assertEqual(driver.added_cookies, [])


class RunTestCase(unittest.TestCase):
    """主流程测试"""

    def setUp(self):
        # run() 签到前有 SITE_GAP 随机延迟（默认 60-180 秒），所有用例统一屏蔽，
        # 避免单测真实 sleep 拖慢执行；专门验证延迟的用例再单独断言。
        self.sleep_mock = mock.patch.object(daily.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        import os

        os.environ.pop("LINUXSB_COOKIE", None)

    def test_未配置cookie时静默跳过且不通知(self):
        with mock.patch.object(daily.notify, "send") as send_mock, \
             mock.patch.object(daily.time, "sleep") as sleep_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        send_mock.assert_not_called()
        sleep_mock.assert_not_called()

    def test_多账号部分失败时退出码为1且单个失败不中断(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1&b=2"

        def get_handler(url, headers=None, cookies=None, timeout=None):
            # 账号1 Cookie 失效，账号2 正常
            if cookies and cookies.get("a") == "1":
                return FakeResponse(text="<html>请登录</html>")
            return FakeResponse(text=PAGE_UNCHECKED)

        with mock.patch.object(daily.requests, "get", side_effect=get_handler), \
             fake_post({"ok": 1, "message": "签到成功"}) as post_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 1)
        # 只有账号2发起了签到 POST
        self.assertEqual(post_mock.call_count, 1)
        # 通知标题带「签到异常」，正文包含失败账号
        title, content = send_mock.call_args.args
        self.assertIn("签到异常", title)
        self.assertIn("账号 1", content)

    def test_所有账号成功时退出码为0(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1&b=2"
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 1, "message": "好"}), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        self.assertEqual(send_mock.call_args.args[0], "LinuxSB 每日任务")

    def test_通知内容对齐nodeseek分段格式(self):
        """通知含各账号签到时间与概览；【linux.sb】域名不进通知"""
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1"
        page = ('<div>当前积分：888</div><div>连续签到 3 天</div>'
                '<input type="hidden" name="_csrf" value="abc">')
        with fake_get(page), fake_post({"ok": 1, "message": ""}), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        content = send_mock.call_args.args[1]
        self.assertIn("签到时间: ", content)
        self.assertNotIn("【linux.sb】", content)
        self.assertNotIn("linux.sb", content)  # 站点域名不进入通知
        self.assertIn("账号 1", content)
        self.assertIn("签到结果: 签到成功", content)
        self.assertIn("当前积分: 888", content)
        self.assertIn("连续签到: 3 天", content)

    def test_日志不输出用户名只输出账号号(self):
        """用户名只进通知；日志（公开仓库 Actions 页面可见）只显示「账号 N」"""
        import os
        from unittest.mock import patch

        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc; bbs_csrf=csrf123"
        page = ('<a class="user-name" href="/user/42">秦昭襄王</a>'
                '<div>当前积分：888</div>'
                '<input type="hidden" name="_csrf" value="abc">')
        out = __import__("io").StringIO()
        with fake_get(page), fake_post({"ok": 1, "message": "好"}), \
             patch("sys.stdout", new=out), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        # 日志：有账号 1，无用户名、无 cookie 键名/值
        self.assertIn("账号 1", out.getvalue())
        self.assertNotIn("秦昭襄王", out.getvalue())
        self.assertNotIn("bbs_auth", out.getvalue())
        self.assertNotIn("bbs_csrf", out.getvalue())
        # 通知：用户名在，cookie 不在
        self.assertIn("秦昭襄王", send_mock.call_args.args[1])
        self.assertNotIn("bbs_auth", send_mock.call_args.args[1])

    def test_post响应redirect字段不展示(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1"
        with fake_get(PAGE_UNCHECKED), \
             fake_post({"ok": 1, "message": "", "redirect": "/daily_checkin", "bonus": 10}), \
             mock.patch.object(daily.notify, "send") as send_mock:
            daily.run()
        content = send_mock.call_args.args[1]
        self.assertNotIn("redirect", content)
        self.assertIn("bonus: 10", content)

    def test_签到前有随机延迟(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1"
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 1, "message": "好"}), \
             mock.patch.object(daily.notify, "send"):
            daily.run()
        self.sleep_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)