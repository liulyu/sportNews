"""DeepSeek 接入验证:抓真实帖子正文 → 调 DeepSeek → 打印生成的 summary。

用法:
  $env:LLM_API_KEY = "sk-你的key"   # PowerShell
  python verify_deepseek.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from scrapers import fetch_hupu, fetch_hupu_detail, fetch_zhibo8, fetch_zhibo8_detail
from state import State
from llm import llm_judge, LLM_API_BASE, LLM_MODEL

print(f"[verify] LLM_API_BASE = {LLM_API_BASE}")
print(f"[verify] LLM_MODEL    = {LLM_MODEL}")
print(f"[verify] LLM_API_KEY  = {'已配(' + os.getenv('LLM_API_KEY', '')[:8] + '...)' if os.getenv('LLM_API_KEY') else '未配'}")
print()

if not os.getenv("LLM_API_KEY"):
    print("[verify] 未配 LLM_API_KEY 环境变量,无法测试。")
    print("        PowerShell: $env:LLM_API_KEY = 'sk-你的key'")
    sys.exit(1)

# 抓一个真实热帖
print("[verify] 抓取虎扑 all-nba ...")
items = fetch_hupu("all-nba", "NBA")
if not items:
    print("[verify] 抓取失败")
    sys.exit(1)

# 取回复数最多的一条
top = max(items, key=lambda x: x.replies)
print(f"[verify] 选定热帖: {top.title}")
print(f"         replies={top.replies} likes={top.likes} | {top.url}")
print()

# 抓详情页正文
print("[verify] 抓详情页正文 ...")
top.content = fetch_hupu_detail(top.url)
print(f"[verify] 正文 {len(top.content)} 字:")
print(top.content[:400])
print("..." if len(top.content) > 400 else "")
print()

# 调 DeepSeek
print(f"[verify] 调用 {LLM_MODEL} ...")
state = State.load("data/state.json") if os.path.exists("data/state.json") else State()
state.health = {"last_success": "", "consecutive_scrape_failures": 0}
state.daily_llm = {}

result = llm_judge(top, state, llm_daily_limit=50)

print()
print("=" * 60)
print("[verify] LLM 返回:")
print(f"  important : {result.get('important')}")
print(f"  score     : {result.get('score')}")
print(f"  headline  : {result.get('headline')}")
print(f"  summary   : {result.get('summary')}")
if result.get("_fallback"):
    print(f"  ⚠ 走降级路径: {result['_fallback']}")
    print("  请检查 API_KEY 是否正确、网络是否可达 api.deepseek.com")
print("=" * 60)

# 模拟推送内容格式
print()
print("[verify] 微信推送将看到的内容:")
print(f"## {result.get('headline', '')}")
print()
print(result.get("summary", ""))
print()
print(f"📊 热度:虎扑 {top.replies}回复 {top.likes}亮")
print(f"🔗 查看原文: {top.url}")
print(f"来源:hupu · NBA")
