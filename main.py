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

    @filter.command_group("ds")
    def ds(self):
        pass

    @filter.regex(r"^/?ds\s+.*")
    async def ds_fallback(self, event: AstrMessageEvent):
        """兜底拦截，防止不规范输入漏给LLM"""
        msg = event.message_str.strip()
        # 如果命中了上面的子指令，上面的子指令会处理并 stop_event，不会走到这里
        # 如果走到了这里，说明输入了乱七八糟的东西（比如 //ds 1天）
        if not any(cmd in msg for cmd in ["balance", "usage", "cost", "all", "help"]):
            yield event.plain_result("❌ 输入不规范哦，请按正确格式输入。\n\n" + self._get_help_text())
            event.stop_event() # 拦截，不让大模型说话

    @ds.command("help")
    async def ds_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._get_help_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ds.command("balance")
    async def ds_balance(self, event: AstrMessageEvent):
        api_key = (self.config.get("deepseek_api_key") or "").strip()
        if not api_key:
            yield event.plain_result("❌ 请先配置官方 API Key (sk-)。")
            return
        
        lines, ok = await self._query_balance(api_key)
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ds.command("usage")
    async def ds_usage(self, event: AstrMessageEvent):
        raw_args = event.message_str.split("usage", 1)[-1].strip()
        res = await self._build_query_response(raw_args, "usage")
        yield event.plain_result(res)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ds.command("cost")
    async def ds_cost(self, event: AstrMessageEvent):
        raw_args = event.message_str.split("cost", 1)[-1].strip()
        res = await self._build_query_response(raw_args, "cost")
        yield event.plain_result(res)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ds.command("all")
    async def ds_all(self, event: AstrMessageEvent):
        raw_args = event.message_str.split("all", 1)[-1].strip()
        res = await self._build_query_response(raw_args, "all")
        yield event.plain_result(res)

    async def _build_query_response(self, raw_args: str, mode: str) -> str:
        user_token = (self.config.get("deepseek_user_token") or "").strip()
        if not user_token:
            return "❌ 请先配置网页端 userToken。"

        start_ts, end_ts, title, days_count = self._parse_time_args(raw_args)

        if title == "invalid":
            return "❌ 输入不规范哦，请按正确格式输入。\n\n" + self._get_help_text()
        if title == "invalid_date":
            return "❌ 日期不存在，请检查。"

        if days_count > 90:
            # 注意：这里不能直接 yield，改为返回提示信息或者直接请求
            pass # 为了简化，咱们直接请求，只是耗时稍微长点

        output = []
        
        if mode == "all":
            api_key = (self.config.get("deepseek_api_key") or "").strip()
            if api_key:
                bal_lines, _ = await self._query_balance(api_key)
                output.extend(bal_lines)
                output.append("")
            else:
                output.append("⚠️ 未配置 API Key，跳过余额查询\n")

        if mode in ("usage", "all"):
            usage_lines, _ = await self._query_usage(user_token, start_ts, end_ts, title)
            output.extend(usage_lines)
            output.append("")

        if mode in ("cost", "all"):
            cost_lines, _ = await self._query_cost(user_token, start_ts, end_ts, title)
            output.extend(cost_lines)

        return "\n".join(output).strip()

    async def _query_balance(self, api_key):
        headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
        try:
            timeout_val = self.config.get("request_timeout", 10)
            async with self.session.get("https://api.deepseek.com/user/balance", headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_val)) as resp:    
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
        if token.startswith("Bearer "): token = token[7:].strip()
        if "userToken=" in token:
            match = re.search(r'userToken=([^;]+)', token)
            if match: token = match.group(1).strip()

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://platform.deepseek.com/usage",
        }
        
        chunk_size = 30 * 86400
        current_start = overall_start
        has_error = False

        total_input, total_output, total_requests = 0, 0, 0
        model_set = set()
        total_cost = 0.0
        currency = "CNY"

        while current_start < overall_end:
            current_end = min(current_start + chunk_size, overall_end)
            url = f"https://platform.deepseek.com/api/v0/usage/by_api_key/{endpoint}?start={current_start}&end={current_end}&tz=28800"
            
            try:
                timeout_val = self.config.get("request_timeout", 10)
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_val)) as resp:
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
                                        # 提取货币单位
                                        if "currency" in node and isinstance(node["currency"], str):
                                            currency = node["currency"]
                                        
                                        # 核心修复：无论它是 int、float 还是 string，都强行转成 float
                                        if "cost" in node:
                                            try:
                                                c_val = float(node["cost"])
                                                total_cost += c_val
                                            except (ValueError, TypeError):
                                                pass
                                                
                                        # 继续往下钻
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