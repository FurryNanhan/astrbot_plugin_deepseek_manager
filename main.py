import aiohttp
import datetime
import asyncio
import re
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger


class DeepSeekUsagePlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.session = None

    async def initialize(self):
        self.session = aiohttp.ClientSession()

    async def terminate(self):
        if self.session:
            await self.session.close()

    def _format_number(self, num) -> str:
        if isinstance(num, str): return num
        fmt = self.config.get("number_format", "comma")
        if fmt == "short":
            if num >= 1_000_000_000: return f"{num / 1_000_000_000:.2f} B"
            elif num >= 1_000_000: return f"{num / 1_000_000:.2f} M"
            elif num >= 1_000: return f"{num / 1_000:.2f} K"
            return str(num)
        return f"{num:,}"

    def _parse_time_args(self, raw_args: str):
        """通用解析时间参数，返回 (start_ts, end_ts, title, days_count)"""
        args = raw_args.split() if raw_args else []
        default_days = self.config.get("default_days", 30)
        
        start_ts, end_ts, title, days_count = None, None, "", 0

        if not args:
            days_count = default_days
            title = f"最近 {days_count} 天"
        elif len(args) == 1:
            arg = args[0].lower()
            if arg == "today":
                days_count = 1
                title = "今天"
            elif arg == "month":
                today = datetime.date.today()
                first_day = today.replace(day=1)
                start_ts = int(datetime.datetime.combine(first_day, datetime.time.min).timestamp())
                end_ts = int(datetime.datetime.combine(today + datetime.timedelta(days=1), datetime.time.min).timestamp())
                title = "本月"
            elif arg.isdigit():
                days_count = int(arg)
                title = f"最近 {days_count} 天"
            else:
                return None, None, "invalid", 0
        elif len(args) == 2:
            date_pattern = r'^(\d{1,2})[\.-](\d{1,2})$'
            m1, m2 = re.match(date_pattern, args[0]), re.match(date_pattern, args[1])
            if m1 and m2:
                current_year = datetime.date.today().year
                try:
                    start_date = datetime.date(current_year, int(m1.group(1)), int(m1.group(2)))
                    end_date = datetime.date(current_year, int(m2.group(1)), int(m2.group(2)))
                    if start_date > end_date: # 跨年
                        start_date = datetime.date(current_year - 1, int(m1.group(1)), int(m1.group(2)))
                    start_ts = int(datetime.datetime.combine(start_date, datetime.time.min).timestamp())
                    end_ts = int(datetime.datetime.combine(end_date + datetime.timedelta(days=1), datetime.time.min).timestamp())
                    title = f"{start_date.strftime('%Y.%m.%d')} 至 {end_date.strftime('%Y.%m.%d')}"
                    days_count = (end_date - start_date).days + 1
                except ValueError:
                    return None, None, "invalid_date", 0
            else:
                return None, None, "invalid", 0
        else:
            return None, None, "invalid", 0

        if start_ts is None or end_ts is None:
            tz = datetime.timezone(datetime.timedelta(hours=8))
            now = datetime.datetime.now(tz)
            end_dt = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz)
            start_dt = end_dt - datetime.timedelta(days=days_count)
            start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())

        return start_ts, end_ts, title, days_count

    def _get_help_text(self):
        return (
            "📖 DeepSeek 管家帮助\n"
            "━━━━━━━━━━━━━━\n"
            "🔹 /ds balance : 查余额 (需 API Key)\n"
            "🔹 /ds usage [天数/范围] : 查 Token 用量\n"
            "🔹 /ds cost [天数/范围] : 查消费金额\n"
            "🔹 /ds all [天数/范围] : 查全部信息\n"
            "   * 范围格式：10.01 10.31\n"
            "   * 天数示例：7 / today / month\n"
            "━━━━━━━━━━━━━━"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command_group("ds")
    def ds(self):
        pass

    @ds.command("help")
    async def ds_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._get_help_text())

    @ds.command("balance")
    async def ds_balance(self, event: AstrMessageEvent):
        api_key = (self.config.get("deepseek_api_key") or "").strip()
        if not api_key:
            yield event.plain_result("❌ 请先配置官方 API Key (sk-)。")
            return
        
        lines, ok = await self._query_balance(api_key)
        yield event.plain_result("\n".join(lines))

    @ds.command("usage")
    async def ds_usage(self, event: AstrMessageEvent):
        await self._handle_platform_query(event, "usage")

    @ds.command("cost")
    async def ds_cost(self, event: AstrMessageEvent):
        await self._handle_platform_query(event, "cost")

    @ds.command("all")
    async def ds_all(self, event: AstrMessageEvent):
        await self._handle_platform_query(event, "all")

    async def _handle_platform_query(self, event: AstrMessageEvent, mode: str):
        user_token = (self.config.get("deepseek_user_token") or "").strip()
        if not user_token:
            yield event.plain_result("❌ 请先配置网页端 userToken。")
            return

        raw_args = event.message_str.split("ds", 1)[-1].replace(mode, "", 1).strip()
        start_ts, end_ts, title, days_count = self._parse_time_args(raw_args)

        if title == "invalid":
            yield event.plain_result("❌ 输入不规范哦，请按正确格式输入。\n\n" + self._get_help_text())
            return
        if title == "invalid_date":
            yield event.plain_result("❌ 日期不存在，请检查。")
            return

        if days_count > 90:
            yield event.plain_result(f"⏳ 跨度 {days_count} 天，分段查询中，请稍候...")

        output = []
        
        # 1. 查余额 (如果 mode 是 all)
        if mode == "all":
            api_key = (self.config.get("deepseek_api_key") or "").strip()
            if api_key:
                bal_lines, _ = await self._query_balance(api_key)
                output.extend(bal_lines)
                output.append("")
            else:
                output.append("⚠️ 未配置 API Key，跳过余额查询\n")

        # 2. 查用量 (usage / all)
        if mode in ("usage", "all"):
            usage_lines, _ = await self._query_usage(user_token, start_ts, end_ts, title)
            output.extend(usage_lines)
            output.append("")

        # 3. 查金额 (cost / all)
        if mode in ("cost", "all"):
            cost_lines, _ = await self._query_cost(user_token, start_ts, end_ts, title)
            output.extend(cost_lines)

        yield event.plain_result("\n".join(output).strip())

    async def _query_balance(self, api_key):
        headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
        try:
            async with self.session.get("https://api.deepseek.com/user/balance", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 401: return (["❌ API Key 无效"], False)
                if resp.status != 200: return ([f"❌ 查询失败，状态码 {resp.status}"], False)
                data = await resp.json()
        except Exception as e:
            return ([f"❌ 网络错误：{type(e).__name__}"], False)

        if not data.get("is_available") or not data.get("balance_infos"):
            return (["当前账户无可用余额。"], False)

        lines = ["📊 DeepSeek 账户余额", "━━━━━━━━━━━━━━"]
        for info in data["balance_infos"]:
            symbol = "¥" if info.get("currency") == "CNY" else "$"
            lines.append(f"💰 总余额：{symbol}{info.get('total_balance', '0')} {info.get('currency')}")
            lines.append(f"   💳 充值余额：{symbol}{info.get('topped_up_balance', '0')}")
        lines.append("━━━━━━━━━━━━━━")
        return (lines, True)

    async def _query_usage(self, token, start_ts, end_ts, title):
        return await self._fetch_and_parse(token, start_ts, end_ts, title, "amount")

    async def _query_cost(self, token, start_ts, end_ts, title):
        return await self._fetch_and_parse(token, start_ts, end_ts, title, "cost")

    async def _fetch_and_parse(self, token, overall_start, overall_end, title, endpoint):
        token = token.strip()
        
        # 1. 如果用户填了 "Bearer xxx"
        if token.startswith("Bearer "):
            token = token[7:].strip()
        
        # 2. 如果用户把整个 Cookie 甚至 "userToken=xxx; other=yyy" 复制进来了
        if "userToken=" in token:
            match = re.search(r'userToken=([^;]+)', token)
            if match:
                token = match.group(1).strip()

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}", # 这里统一加上标准的 Bearer
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://platform.deepseek.com/usage",
        }
        
        chunk_size = 30 * 86400
        current_start = overall_start
        has_error = False

        # 用量数据
        total_input, total_output, total_requests = 0, 0, 0
        model_set = set()
        # 消费数据
        total_cost = 0.0
        currency = "CNY"

        while current_start < overall_end:
            current_end = min(current_start + chunk_size, overall_end)
            url = f"https://platform.deepseek.com/api/v0/usage/by_api_key/{endpoint}?start={current_start}&end={current_end}&tz=28800"
            
            try:
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and data.get("code") == 0:
                            biz_data = data.get("data", {}).get("biz_data", {}) or {}
                            
                            if endpoint == "amount":
                                for s in biz_data.get("series", []):
                                    model_set.add(s.get("model", "Unknown"))
                                    for b in s.get("buckets", []):
                                        u = b.get("usage", {})
                                        total_input += (u.get("PROMPT_CACHE_HIT_TOKEN", 0) + u.get("PROMPT_CACHE_MISS_TOKEN", 0))
                                        total_output += u.get("RESPONSE_TOKEN", 0)
                                        total_requests += u.get("REQUEST", 0)
                            elif endpoint == "cost":
                                def find_cost(node):
                                    nonlocal total_cost, currency
                                    if isinstance(node, dict):
                                        if "cost" in node and isinstance(node["cost"], (int, float)): total_cost += node["cost"]
                                        if "total_cost" in node and isinstance(node["total_cost"], (int, float)): total_cost += node["total_cost"]
                                        if "currency" in node and isinstance(node["currency"], str): currency = node["currency"]
                                        for v in node.values(): find_cost(v)
                                    elif isinstance(node, list):
                                        for i in node: find_cost(i)
                                find_cost(biz_data)
                        else:
                            logger.warning(f"接口 {endpoint} 返回异常: {data}")
                    else:
                        logger.error(f"接口 {endpoint} 请求失败: {resp.status}")
                        has_error = True
            except Exception as e:
                logger.error(f"请求出错: {e}")
                has_error = True
            
            current_start = current_end
            await asyncio.sleep(0.5)

        if endpoint == "amount":
            tot = total_input + total_output
            if tot == 0: return ([f"📭 {title} 无 Token 记录"], True)
            lines = [
                f"📈 Token 用量（{title}）",
                "━━━━━━━━━━━━━━",
                f"🤖 模型：{', '.join(model_set)}",
                f"🔤 总量：{self._format_number(tot)}",
                f"   ⬆️ 输入：{self._format_number(total_input)}",
                f"   ⬇️ 输出：{self._format_number(total_output)}",
                f"🔁 请求：{self._format_number(total_requests)} 次",
            ]
        else:
            if total_cost == 0: return ([f"📭 {title} 无消费记录"], True)
            symbol = "¥" if currency == "CNY" else "$"
            lines = [
                f"💸 消费金额（{title}）",
                "━━━━━━━━━━━━━━",
                f"💰 总消费：{symbol}{total_cost:.4f}",
            ]

        if has_error: lines.append("⚠️ 部分数据请求失败，可能不完整")
        lines.append("━━━━━━━━━━━━━━")
        return (lines, True)