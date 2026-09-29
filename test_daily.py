# -- coding: utf-8 --
"""主脚本配置解析与通知正文测试。

nodeseek_daily 依赖 selenium / undetected_chromedriver 等浏览器库，
本地与 CI 的单测环境不一定安装，这里用桩模块替换后再导入，
使开关逻辑与正文拼装可以脱离浏览器独立验证。
"""
import contextlib
import io
import sys
import types
import unittest
from unittest import mock


def _install_stub_modules():
    """为浏览器相关依赖注册最小桩模块，仅满足 import 期需要的属性访问。"""
    if "nodeseek_daily" in sys.modules:
        return

    def stub(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules.setdefault(name, module)
        return module

    class _Anything:
        """任意属性访问都返回自身，覆盖 By.XPATH、EC.xxx 之类的用法。"""

        def __getattr__(self, item):
            return self

        def __call__(self, *args, **kwargs):
            return self

    stub("undetected_chromedriver", Chrome=_Anything(), ChromeOptions=_Anything())
    stub("bs4", BeautifulSoup=_Anything())

    stub("selenium")
    stub("selenium.webdriver")
    stub("selenium.webdriver.common")
    stub("selenium.webdriver.common.by", By=_Anything())
    stub("selenium.webdriver.common.keys", Keys=_Anything())
    stub("selenium.webdriver.common.action_chains", ActionChains=_Anything())
    stub("selenium.webdriver.support")
    stub("selenium.webdriver.support.ui", WebDriverWait=_Anything())
    stub("selenium.webdriver.support.expected_conditions", presence_of_element_located=_Anything())


_install_stub_modules()

import nodeseek_daily as daily  # noqa: E402  桩模块必须先安装


class EnvBoolTestCase(unittest.TestCase):
    """校验布尔环境变量解析，避免出现 NS_RANDOM="false" 被判为真的老问题。"""

    def test_真值写法全部识别为真(self):
        for raw in ("true", "TRUE", "True", "1", "yes", "on", "y", " true "):
            with mock.patch.dict("os.environ", {"NS_TEST_FLAG": raw}):
                self.assertTrue(daily.env_bool("NS_TEST_FLAG"), f"{raw!r} 应为真")

    def test_假值写法全部识别为假(self):
        for raw in ("false", "FALSE", "0", "no", "off", "随便写"):
            with mock.patch.dict("os.environ", {"NS_TEST_FLAG": raw}):
                self.assertFalse(daily.env_bool("NS_TEST_FLAG"), f"{raw!r} 应为假")

    def test_未设置或空串时取默认值(self):
        with mock.patch.dict("os.environ", {"NS_TEST_FLAG": ""}):
            self.assertFalse(daily.env_bool("NS_TEST_FLAG"))
            self.assertTrue(daily.env_bool("NS_TEST_FLAG", default=True))

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(daily.env_bool("NS_TEST_FLAG"))
            self.assertTrue(daily.env_bool("NS_TEST_FLAG", default=True))


class ChromeVersionTestCase(unittest.TestCase):
    """校验 Chrome 大版本解析，驱动版本必须与浏览器一致否则无法建立会话。"""

    def test_解析标准版本输出(self):
        self.assertEqual(daily.parse_chrome_major_version("Google Chrome 150.0.7871.128"), 150)
        self.assertEqual(daily.parse_chrome_major_version("Chromium 151.0.1.2"), 151)

    def test_无法解析时返回None(self):
        for raw in ("", None, "no version here"):
            self.assertIsNone(daily.parse_chrome_major_version(raw), f"{raw!r} 应返回 None")

    def test_环境变量可覆盖探测结果(self):
        with mock.patch.dict("os.environ", {"CHROME_MAJOR_VERSION": "149"}):
            self.assertEqual(daily.detect_chrome_major_version(), 149)

    def test_环境变量非数字时忽略并继续探测(self):
        with mock.patch.dict("os.environ", {"CHROME_MAJOR_VERSION": "abc"}), \
                mock.patch("shutil.which", return_value=None):
            self.assertIsNone(daily.detect_chrome_major_version())


def _site_result(site, sign, comment_stats, summary, started_at="2026-07-30 08:00:00"):
    """构造 build_notify_content 需要的单站结果元组。"""
    return (site, sign, comment_stats, summary, started_at)


class BuildNotifyContentTestCase(unittest.TestCase):
    """校验通知正文在三种结果下的表述。单站与多站排版都要覆盖。"""

    SITE = daily.Site("NodeSeek", "nodeseek.com", "session=x")
    SIGN_OK = {"success": True, "detail": "签到成功，获得 5 个鸡腿"}

    def test_附加任务关闭时说明已关闭且不输出统计(self):
        content = daily.build_notify_content([_site_result(self.SITE, self.SIGN_OK, None, {})])
        self.assertIn("签到成功", content)
        self.assertIn("已关闭", content)
        self.assertNotIn("0/0", content)
        self.assertNotIn("加鸡腿", content)

    def test_附加任务开启时输出评论与鸡腿统计(self):
        stats = {"total": 20, "commented": 18, "chicken_leg": True, "error": ""}
        content = daily.build_notify_content([_site_result(self.SITE, self.SIGN_OK, stats, {})])
        self.assertIn("成功 18/20 个帖子", content)
        self.assertIn("加鸡腿: 成功", content)

    def test_评论异常时正文体现异常原因(self):
        stats = {"total": 0, "commented": 0, "chicken_leg": False, "error": "TimeoutException 超时"}
        content = daily.build_notify_content(
            [_site_result(self.SITE, {"success": False, "detail": "签到失败"}, stats, {})]
        )
        self.assertIn("异常终止", content)
        self.assertIn("TimeoutException", content)
        self.assertIn("加鸡腿: 未成功", content)

    def test_账号概览字段进入通知(self):
        summary = {"level": "1", "chicken_leg": "118", "comment": "4", "topic": "2"}
        content = daily.build_notify_content([_site_result(self.SITE, self.SIGN_OK, None, summary)])
        self.assertIn("当前等级: Lv 1", content)
        self.assertIn("总鸡腿数: 118", content)
        self.assertIn("评论数: 4", content)
        self.assertIn("主题贴数: 2", content)

    def test_多站点分段显示且空行分隔(self):
        ns = self.SITE
        df = daily.Site("DeepFlood", "deepflood.com", "session=y")
        content = daily.build_notify_content([
            _site_result(ns, self.SIGN_OK, None, {"level": "1", "chicken_leg": "118"}),
            _site_result(df, {"success": True, "detail": "签到成功，获得 5 个鸡腿"}, None, {"level": "2", "chicken_leg": "50"}),
        ])
        self.assertIn("【NodeSeek】", content)
        self.assertIn("【DeepFlood】", content)
        self.assertIn("总鸡腿数: 118", content)
        self.assertIn("总鸡腿数: 50", content)
        # 两站之间应有空行
        self.assertIn("\n\n【DeepFlood】", content)


class RunTestCase(unittest.TestCase):
    """校验 NS_EXTRA_TASKS 开关真正决定评论任务是否被调用。"""

    SIGN_OK = {"success": True, "detail": "签到成功"}
    SITE = daily.Site("NodeSeek", "nodeseek.com", "session=x")
    DRIVER = object()

    def _patch_run_env(self, **overrides):
        """统一 mock 出 run 所需的外部依赖。"""
        patches = {
            "load_sites": mock.patch.object(daily, "load_sites", return_value=[self.SITE]),
            "create_driver": mock.patch.object(daily, "create_driver", return_value=self.DRIVER),
            "inject_site_cookies": mock.patch.object(daily, "inject_site_cookies", return_value=True),
            "nodeseek_comment": mock.patch.object(daily, "nodeseek_comment"),
            "click_sign_icon": mock.patch.object(daily, "click_sign_icon", return_value=self.SIGN_OK),
            "fetch_account_summary": mock.patch.object(daily, "fetch_account_summary", return_value={}),
            "send": mock.patch.object(daily.notify, "send"),
        }
        for key, patcher in patches.items():
            overrides.setdefault(key, patcher)
        return overrides

    def test_开关关闭时不调用评论任务(self):
        m = self._patch_run_env()
        with mock.patch.object(daily, "extra_tasks_enabled", False), \
                m["load_sites"], m["create_driver"], m["inject_site_cookies"], \
                m["nodeseek_comment"] as comment, m["click_sign_icon"], \
                m["fetch_account_summary"], m["send"] as send:
            code = daily.run()

        comment.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("已关闭", send.call_args.args[1])

    def test_开关开启时调用评论任务(self):
        stats = {"total": 20, "commented": 20, "chicken_leg": True, "error": ""}
        m = self._patch_run_env(nodeseek_comment=mock.patch.object(daily, "nodeseek_comment", return_value=stats))
        with mock.patch.object(daily, "extra_tasks_enabled", True), \
                m["load_sites"], m["create_driver"], m["inject_site_cookies"], \
                m["nodeseek_comment"] as comment, m["click_sign_icon"], \
                m["fetch_account_summary"], m["send"] as send:
            code = daily.run()

        comment.assert_called_once()
        self.assertEqual(code, 0)
        self.assertIn("20/20", send.call_args.args[1])

    def test_浏览器初始化失败时推送失败通知且不执行任务(self):
        m = self._patch_run_env(create_driver=mock.patch.object(daily, "create_driver", return_value=None))
        with mock.patch.object(daily, "extra_tasks_enabled", True), \
                m["load_sites"], m["create_driver"], m["inject_site_cookies"], \
                m["nodeseek_comment"] as comment, m["click_sign_icon"] as sign, \
                m["fetch_account_summary"], m["send"] as send:
            code = daily.run()

        comment.assert_not_called()
        sign.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn("失败", send.call_args.args[0])

    def test_签到失败时退出码为1(self):
        m = self._patch_run_env(click_sign_icon=mock.patch.object(
            daily, "click_sign_icon", return_value={"success": False, "detail": "签到失败: 超时"}))
        with mock.patch.object(daily, "extra_tasks_enabled", False), \
                m["load_sites"], m["create_driver"], m["inject_site_cookies"], \
                m["nodeseek_comment"], m["click_sign_icon"], \
                m["fetch_account_summary"], m["send"] as send:
            code = daily.run()

        self.assertEqual(code, 1)
        self.assertIn("签到异常", send.call_args.args[0])

    def test_抓到账号概览时先确认登录态再签到(self):
        m = self._patch_run_env(fetch_account_summary=mock.patch.object(
            daily, "fetch_account_summary", return_value={"level": "1", "chicken_leg": "118"}))
        with mock.patch.object(daily, "extra_tasks_enabled", False), \
                m["load_sites"], m["create_driver"], m["inject_site_cookies"], \
                m["nodeseek_comment"], m["click_sign_icon"] as sign, \
                m["fetch_account_summary"], m["send"] as send:
            code = daily.run()

        self.assertTrue(sign.call_args.kwargs["logged_in"])
        self.assertEqual(code, 0)
        self.assertIn("总鸡腿数: 118", send.call_args.args[1])

    def test_账号概览为空时登录态未确认且通知里显形(self):
        m = self._patch_run_env()
        with mock.patch.object(daily, "extra_tasks_enabled", False), \
                m["load_sites"], m["create_driver"], m["inject_site_cookies"], \
                m["nodeseek_comment"], m["click_sign_icon"] as sign, \
                m["fetch_account_summary"], m["send"] as send:
            code = daily.run()

        self.assertFalse(sign.call_args.kwargs["logged_in"])
        self.assertIn("账号概览: 未抓到", send.call_args.args[1])
        # 签到判定由 click_sign_icon 内部把关，这里被 mock 成成功，仅验证登录态传递
        self.assertEqual(code, 0)

    def test_截断粘贴经试注入确认登录后日志点明无需重贴(self):
        # 无名片段试注入 + 账号概览抓到 = 片段就是被截掉名字的 session
        site = daily.Site("NodeSeek", "nodeseek.com", "0123456789abcdef0123456789abcdef; pjwt=y")
        m = self._patch_run_env(
            load_sites=mock.patch.object(daily, "load_sites", return_value=[site]),
            fetch_account_summary=mock.patch.object(
                daily, "fetch_account_summary", return_value={"level": "1"}),
        )
        log = io.StringIO()
        with mock.patch.object(daily, "extra_tasks_enabled", False), \
                m["load_sites"], m["create_driver"], m["inject_site_cookies"], \
                m["nodeseek_comment"], m["click_sign_icon"], m["fetch_account_summary"], \
                m["send"], contextlib.redirect_stdout(log):
            code = daily.run()

        self.assertEqual(code, 0)
        self.assertIn("登录态已确认：开头的无名片段就是被截掉名字的登录凭据", log.getvalue())

    def test_截断粘贴试注入无效时给出重贴线索(self):
        site = daily.Site("NodeSeek", "nodeseek.com", "0123456789abcdef0123456789abcdef; pjwt=y")
        m = self._patch_run_env(
            load_sites=mock.patch.object(daily, "load_sites", return_value=[site]),
        )
        log = io.StringIO()
        with mock.patch.object(daily, "extra_tasks_enabled", False), \
                m["load_sites"], m["create_driver"], m["inject_site_cookies"], \
                m["nodeseek_comment"], m["click_sign_icon"], m["fetch_account_summary"], \
                m["send"], contextlib.redirect_stdout(log):
            code = daily.run()

        self.assertIn("试注入仍无效", log.getvalue())


class ShouldSkipCookieTestCase(unittest.TestCase):
    """校验 cookie 过滤：环境绑定与统计类 cookie 必须跳过，登录态必须保留。"""

    def test_跳过_cloudflare_与统计类_cookie(self):
        for name in ("cf_clearance", "__cf_bm", "__cflb", "_ga", "_ga_47LDR1H8FC", "_gid", "_gat"):
            self.assertTrue(daily.should_skip_cookie(name), f"{name} 应跳过")

    def test_保留登录态相关_cookie(self):
        for name in ("session", "pjwt", "smac", "fog", "colorscheme"):
            self.assertFalse(daily.should_skip_cookie(name), f"{name} 应注入")

    def test_大小写与空白不影响判定(self):
        self.assertTrue(daily.should_skip_cookie("  CF_Clearance  "))
        self.assertFalse(daily.should_skip_cookie("  Session  "))


class ParseCookieStringTestCase(unittest.TestCase):
    """校验 NS_COOKIE 解析：正常项注入、CF 项跳过、值含分号不被截断、多行粘贴容错。"""

    def _names(self, pairs):
        return [name for name, _ in pairs]

    def test_基本分号分隔并跳过cf项(self):
        pairs, _ = daily.parse_cookie_string("session=abc; cf_clearance=xyz; smac=123")
        self.assertEqual(self._names(pairs), ["session", "smac"])

    def test_值中含分号不被截断(self):
        # 某个 cookie 的值本身含分号（如被截断的 JSON），后半段应拼回而非产生残缺片段
        pairs, skipped = daily.parse_cookie_string("session=a;b;c; smac=1")
        self.assertEqual(self._names(pairs), ["session", "smac"])
        session_value = dict(pairs)["session"]
        self.assertEqual(session_value, "a;b;c")
        # 不应因值里的分号报告异常片段
        self.assertEqual(skipped, [])

    def test_换行作为分隔符(self):
        pairs, _ = daily.parse_cookie_string("session=abc\nsmac=123\r\npjwt=xyz")
        self.assertEqual(self._names(pairs), ["session", "smac", "pjwt"])

    def test_跳过原因不含cookie值(self):
        _, skipped = daily.parse_cookie_string("cf_clearance=secretvalue; session=x")
        joined = " ".join(skipped)
        self.assertIn("cf_clearance", joined)
        self.assertNotIn("secretvalue", joined)

    def test_空输入返回空列表(self):
        pairs, skipped = daily.parse_cookie_string("")
        self.assertEqual(pairs, [])
        self.assertEqual(skipped, [])

    def test_开头异常片段提示重新复制(self):
        # 开头 32 字符没有合法 cookie 名，说明粘贴被截断，必须给出可操作的提示
        _, skipped = daily.parse_cookie_string("0123456789abcdef0123456789abcdef; session=x")
        self.assertTrue(any("重新复制" in reason for reason in skipped), skipped)

    def test_异常片段提示包含形状但不含内容(self):
        # 形状（长度/是否含等号/非法字符类别）用于定位问题来源，内容不得进日志
        _, skipped = daily.parse_cookie_string("abc0123456789abcdef01234567; session=x")
        joined = " ".join(skipped)
        self.assertIn("长度 27", joined)
        self.assertIn("不含等号", joined)

    def test_去掉开头的BOM(self):
        # 记事本保存的 UTF-8 文本带 BOM，粘贴进 Secret 后首个 cookie 名会被污染
        pairs, skipped = daily.parse_cookie_string("﻿session=abc; smac=1")
        self.assertEqual(self._names(pairs), ["session", "smac"])
        self.assertEqual(skipped, [])


class StripCookieWrappersTestCase(unittest.TestCase):
    """校验复制包裹剥离。

    包裹物会让首个 cookie 名非法而被整条丢弃：若丢的是登录字段，
    浏览器就停在未登录状态，页面上的"今日签到"入口文案会被误读成已签到。
    """

    def _names(self, raw):
        pairs, _ = daily.parse_cookie_string(raw)
        return [name for name, _ in pairs]

    def test_成对引号被剥离(self):
        for raw in ("\"session=abc; pjwt=y\"", "'session=abc; pjwt=y'"):
            self.assertEqual(self._names(raw), ["session", "pjwt"], raw)

    def test_请求头前缀被剥离(self):
        for raw in ("Cookie: session=abc; pjwt=y", "Set-Cookie: session=abc; pjwt=y"):
            self.assertEqual(self._names(raw), ["session", "pjwt"], raw)

    def test_curl参数名被剥离(self):
        for raw in ("-H 'Cookie: session=abc; pjwt=y'", "--cookie \"session=abc; pjwt=y\"",
                    "-b session=abc; pjwt=y"):
            self.assertEqual(self._names(raw), ["session", "pjwt"], raw)

    def test_BOM与引号叠加仍可解析(self):
        self.assertEqual(self._names("﻿\"session=abc; pjwt=y\""), ["session", "pjwt"])

    def test_不可见字符被清理(self):
        # 零宽空格等不可见字符同样只可能来自复制，夹在 cookie 名里会让整条被丢弃
        for invisible in ("\ufeff", "\u200b", "\u200c", "\u200d"):
            raw = f"{invisible}session=abc; pjwt=y"
            self.assertEqual(self._names(raw), ["session", "pjwt"], repr(raw))

    def test_剥离后cookie值保持不变(self):
        # 剥离只动外层包装，值里的分号要继续按既有规则拼回
        pairs, _ = daily.parse_cookie_string('"session=a;b;c; smac=1"')
        self.assertEqual(dict(pairs)["session"], "a;b;c")

    def test_正常cookie串不受影响(self):
        self.assertEqual(self._names("session=abc; pjwt=y"), ["session", "pjwt"])


class CookieLoginFieldTestCase(unittest.TestCase):
    """校验登录态字段识别：缺 session 的 cookie 串必须能被识别出来。"""

    def _site(self, raw):
        return daily.Site("NodeSeek", "nodeseek.com", raw)

    def test_含_session_视为登录态完整(self):
        self.assertTrue(daily.cookie_has_login(self._site("colorscheme=dark; session=abc; pjwt=xyz")))

    def test_缺_session_视为登录态不完整(self):
        self.assertFalse(daily.cookie_has_login(self._site("pjwt=xyz; smac=1")))

    def test_前缀异常片段不影响鉴权字段识别(self):
        # 2026-09-30 实际形态：粘贴被截断，开头 32 字符没有 cookie 名，session 整条丢失
        raw = "0123456789abcdef0123456789abcdef; cf_clearance=x; pjwt=y; smac=z"
        self.assertFalse(daily.cookie_has_login(self._site(raw)))


class OrphanLoginCandidateTestCase(unittest.TestCase):
    """校验"被截掉名字的登录凭据"候选识别。

    2026-09-30 的运行日志显示开头片段"长度 32，不含等号"，排除了引号/BOM 包裹，
    指向粘贴被从头截断；此时片段很可能就是丢了名字的 session 值，必须作为候选试注入。
    """

    def _site(self, raw):
        return daily.Site("NodeSeek", "nodeseek.com", raw)

    def test_无名片段作为候选返回(self):
        fragment = "0123456789abcdef0123456789abcdef"
        self.assertEqual(daily.orphan_login_candidate(self._site(f"{fragment}; pjwt=y")), fragment)

    def test_正常cookie串没有候选(self):
        self.assertIsNone(daily.orphan_login_candidate(self._site("session=abc; pjwt=y")))
        self.assertIsNone(daily.orphan_login_candidate(self._site("pjwt=xyz; smac=1")))

    def test_包裹剥离后是正常cookie则无候选(self):
        self.assertIsNone(daily.orphan_login_candidate(self._site('"session=abc; pjwt=y"')))

    def test_含空白的片段不算候选(self):
        # cookie 值里不会有空白，出现空白说明片段是说明文字之类的垃圾
        self.assertIsNone(daily.orphan_login_candidate(self._site("某站点说明 abc; pjwt=y")))

    def test_过短片段不算候选(self):
        self.assertIsNone(daily.orphan_login_candidate(self._site("abc; pjwt=y")))

    def test_空cookie没有候选(self):
        self.assertIsNone(daily.orphan_login_candidate(self._site("")))


class _FakeDriver:
    """只提供判定所需属性的最小 driver 桩。"""

    def __init__(self, page_text):
        self._text = page_text
        self.title = "NodeSeek"
        self.current_url = "https://www.nodeseek.com/board"
        self.page_source = "<html></html>"
        self.cookies = []

    def get(self, url):
        self.current_url = url

    def add_cookie(self, cookie):
        self.cookies.append((cookie["name"], cookie["value"]))

    def refresh(self):
        pass


class _TextSoup:
    """把页面源码固定映射为给定文本的 BeautifulSoup 替身。"""

    def __init__(self, text):
        self._text = text

    def get_text(self, *args, **kwargs):
        return self._text


class _AlwaysTimeoutWait:
    """模拟 WebDriverWait 等不到元素时抛异常。"""

    def __init__(self, *args, **kwargs):
        pass

    def until(self, *args, **kwargs):
        raise TimeoutError("元素未出现")


class DetectAlreadySignedTestCase(unittest.TestCase):
    """校验已签到判定口径：泛化的签到入口文案不能被当成已签到。"""

    def _detect(self, text):
        driver = _FakeDriver(text)
        with mock.patch.object(daily, "BeautifulSoup", lambda *a, **k: _TextSoup(text)):
            return daily.detect_already_signed(driver)

    def test_只有入口文案时不算已签到(self):
        # 未签到、甚至未登录的页面都会渲染这些词，命中即误报漏签
        for text in ("今日签到", "已签到", "已经签到", "欢迎回来，今日签到", "今日已签到"):
            self.assertFalse(self._detect(text), f"{text!r} 不应判为已签到")

    def test_显示签到收益时判为已签到(self):
        self.assertTrue(self._detect("今日签到获得鸡腿3个"))
        self.assertTrue(self._detect("签到成功，获得 5 个鸡腿"))

    def test_出现收尾文案时判为已签到(self):
        self.assertTrue(self._detect("今日奖励已领取，请明天再来"))

    def test_未签到的推广文案不算已签到(self):
        # "可获得/可领取"是未然措辞，未签到页面上的推广文案不能当成已签到
        for text in ("每日签到可获得 5 个鸡腿", "签到即可领取鸡腿5个"):
            self.assertFalse(self._detect(text), f"{text!r} 不应判为已签到")


class ClickSignIconTestCase(unittest.TestCase):
    """校验未确认登录态时绝不按"已签到"收尾（2026-09-30 漏签事故回归用例）。"""

    SITE = daily.Site("NodeSeek", "nodeseek.com", "pjwt=xyz; smac=1")

    def _click(self, text, logged_in, site=None):
        driver = _FakeDriver(text)
        with mock.patch.object(daily, "BeautifulSoup", lambda *a, **k: _TextSoup(text)), \
                mock.patch.object(daily, "WebDriverWait", _AlwaysTimeoutWait), \
                mock.patch.object(daily.time, "sleep", lambda *_: None):
            return daily.click_sign_icon(driver, site or self.SITE, logged_in=logged_in)

    def test_未登录时页面签到文案不算已签到(self):
        # 那天的真实形态：cookie 缺 session → 未登录 → 无按钮 + 页面有"今日签到"入口文案
        result = self._click("今日签到", logged_in=False)
        self.assertFalse(result["success"])
        self.assertIn("未确认登录态", result["detail"])
        # 通知只给中性结论：判定不依赖 cookie 名，具体线索打在日志里
        self.assertIn("cookie 可能不完整或已失效", result["detail"])

    def test_已登录且显示收益时仍判已签到(self):
        result = self._click("今日签到获得鸡腿3个", logged_in=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["detail"], "今日已签到")

    def test_已登录但页面无任何签到标志时报失败(self):
        result = self._click("欢迎回来", logged_in=True)
        self.assertFalse(result["success"])
        self.assertIn("未找到领取按钮", result["detail"])

    def test_cookie_完整但未确认登录态时提示失效(self):
        # 判定与 cookie 名无关：即便带了 session，抓不到账号概览也不按已签到收尾
        site = daily.Site("NodeSeek", "nodeseek.com", "session=abc; smac=1")
        result = self._click("今日签到", logged_in=False, site=site)
        self.assertFalse(result["success"])
        self.assertIn("cookie 可能不完整或已失效", result["detail"])

    def test_截断粘贴试注入无效时日志点明粘贴被截断(self):
        # 有可试注入的无名片段却没签到成功，说明片段不是凭据，日志要指明这一点
        site = daily.Site("NodeSeek", "nodeseek.com",
                          "0123456789abcdef0123456789abcdef; pjwt=y")
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            result = self._click("今日签到", logged_in=False, site=site)

        self.assertFalse(result["success"])
        self.assertIn("试注入仍无效，粘贴确实被截断", log.getvalue())


class InjectSiteCookiesTestCase(unittest.TestCase):
    """校验注入阶段：粘贴被从头截断时，要用开头的无名片段补一次登录凭据试注入。"""

    FRAGMENT = "0123456789abcdef0123456789abcdef"

    def _inject(self, raw):
        site = daily.Site("NodeSeek", "nodeseek.com", raw)
        driver = _FakeDriver("")
        log = io.StringIO()
        with mock.patch.object(daily, "wait_for_cloudflare", lambda *a, **k: True), \
                mock.patch.object(daily.time, "sleep", lambda *_: None), \
                contextlib.redirect_stdout(log):
            ok = daily.inject_site_cookies(driver, site)
        return ok, driver, log.getvalue()

    def test_被截断时用无名片段补session(self):
        ok, driver, output = self._inject(f"{self.FRAGMENT}; cf_clearance=x; pjwt=y; smac=z")
        self.assertTrue(ok)
        self.assertEqual(dict(driver.cookies)["session"], self.FRAGMENT)
        self.assertIn("试注入", output)
        # 片段内容属凭据，只允许形状描述进日志
        self.assertNotIn(self.FRAGMENT, output)

    def test_正常cookie串不额外补session(self):
        ok, _, output = self._inject("session=abc; pjwt=y")
        self.assertTrue(ok)
        self.assertNotIn("试注入", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
