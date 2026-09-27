import hmac
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import streamlit as st


SYSTEM_PROMPT = """You are a concise web research assistant.
Search the live web before answering current or factual questions.
Prioritize relevant Polymarket markets, especially for forecasts and prediction-market
questions, and cross-check with reliable reporting when possible. Do not force a
Polymarket connection for unrelated questions.
For a market, identify the exact event, outcome, deadline and available probability.
Include the source's timestamp when available. Distinguish market-implied probability
from established facts. Never invent prices, probabilities, markets or timestamps.
If a current probability cannot be verified, say so; do not present old data as live.
Answer in the language of the user's latest question, using Simplified Chinese for
Chinese questions unless requested otherwise. Keep answers concise and retain citation
markers using the exact search result IDs, such as [1]. Cite only sources that support
the claim. Do not append a long URL list; sources are displayed separately by the app.
"""


def setting(name, default=""):
    value = os.environ.get(name)
    if value is not None:
        return value
    try:
        return str(st.secrets.get(name, default))
    except FileNotFoundError:
        return default


def ask_perplexity(messages, api_key):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    response = requests.post(
        "https://api.perplexity.ai/v1/agent",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "preset": setting("PERPLEXITY_PRESET", "fast"),
            "instructions": SYSTEM_PROMPT + f"\nCurrent time: {today}",
            "input": [
                {"type": "message", "role": m["role"], "content": m["content"]}
                for m in messages
            ],
            "max_output_tokens": 1200,
        },
        timeout=(10, 90),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise ValueError("Incomplete or invalid response")
    parts = []
    citations = []
    for item in data["output"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid output item")
        if item.get("type") == "message" and item.get("role") == "assistant":
            for part in item["content"]:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    parts.append(part["text"])
        elif item.get("type") == "search_results":
            for result in item.get("results", []):
                if isinstance(result, dict):
                    citations.append({"id": result.get("id"), "url": result.get("url")})
    answer = "\n\n".join(parts)
    if not answer.strip():
        raise ValueError("Empty answer")
    return {"role": "assistant", "content": answer, "citations": citations}


def display_message(message):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("citations"):
            with st.expander("信息来源 / Sources"):
                for index, citation in enumerate(message["citations"], 1):
                    # Older messages in an open session contain URL strings.
                    url = citation.get("url") if isinstance(citation, dict) else citation
                    source_id = citation.get("id") if isinstance(citation, dict) else index
                    if isinstance(url, str) and urlparse(url).scheme in ("https", "http"):
                        st.link_button(f"[{source_id}] {urlparse(url).netloc}", url)


st.set_page_config(page_title="预测问答 · Market search", page_icon="🔎")
st.title("预测问答")
st.caption("用中文或英文提问，了解 Polymarket 预测与相关新闻。")
st.session_state.setdefault("authenticated", False)
st.session_state.setdefault("messages", [])

password = setting("APP_PASSWORD")
if not password:
    st.info("应用尚未配置访问密码，请联系管理员。 / APP_PASSWORD is not configured.")
    st.stop()

if not st.session_state.authenticated:
    with st.form("login", clear_on_submit=True):
        entered = st.text_input("访问密码 / Password", type="password")
        submitted = st.form_submit_button("进入 / Sign in", type="primary")
    if submitted:
        if hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
            st.session_state.authenticated = True
            st.rerun()
        st.error("密码不正确，请重试。 / Incorrect password.")
    st.stop()

with st.container(horizontal=True):
    if st.button("新对话 / New chat"):
        st.session_state.messages = []
        st.session_state.pop("search_error", None)
        st.rerun()
    if st.button("退出 / Sign out"):
        st.session_state.clear()
        st.rerun()

api_key = setting("PERPLEXITY_API_KEY")
if not api_key:
    st.info("搜索服务尚未配置，请联系管理员。 / PERPLEXITY_API_KEY is not configured.")
    st.stop()

if not st.session_state.messages:
    st.info("试试：Polymarket 如何预测下一次美联储利率决议？")

for message in st.session_state.messages:
    display_message(message)

pending = bool(st.session_state.messages and st.session_state.messages[-1]["role"] == "user")
if st.session_state.get("search_error"):
    st.error(st.session_state.search_error)
retry = False
if pending and st.button("重试上个问题 / Retry last question"):
    retry = True
prompt = st.chat_input("输入问题 / Ask a question", max_chars=4000, submit_mode="disable")
if prompt and prompt.strip():
    if len(prompt) > 4000:
        st.error("问题请控制在 4000 字符以内。 / Please use at most 4,000 characters.")
        st.stop()
    # Replace a failed question when the user submits a new one.
    if pending:
        st.session_state.messages.pop()
    st.session_state.messages.append({"role": "user", "content": prompt})
    display_message(st.session_state.messages[-1])

if (prompt and prompt.strip()) or retry:
    st.session_state.pop("search_error", None)
    try:
        with st.spinner("正在搜索并整理信息… / Searching…"):
            # Bound request size while retaining the last five complete exchanges.
            answer = ask_perplexity(st.session_state.messages[-11:], api_key)
        st.session_state.messages.append(answer)
        st.rerun()
    except requests.Timeout:
        error = ("搜索超时，请重试。 / Search timed out. Please retry.")
    except requests.HTTPError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            error = ("搜索服务认证失败，请联系管理员检查 API 密钥。 / Check the API key.")
        elif status in (402, 429):
            error = ("搜索额度不足或请求过多，请稍后重试或联系管理员。 / Check quota or retry later.")
        else:
            error = ("搜索服务暂时不可用，请重试。 / Search is unavailable. Please retry.")
    except requests.RequestException:
        error = ("无法连接搜索服务，请重试。 / Connection failed. Please retry.")
    except (ValueError, KeyError, IndexError, TypeError):
        error = ("搜索服务返回了无效结果，请重试。 / Invalid search response. Please retry.")
    st.session_state.search_error = error
    st.rerun()
