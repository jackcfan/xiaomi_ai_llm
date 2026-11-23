"""hub 网页后台：服务端渲染 HTML 字符串，无前端构建、无登录（家庭局域网工具）。

规则：所有输出 HTML 转义；API key 只显示 ``****`` + 后 4 位；编辑供应商时 key 留空表示不修改。
POST 做同源检查：Origin（缺失时看 Referer）的 host:port 与请求 Host 不一致即 403，
防止局域网浏览器打开的任意网页跨站提交（例如改 base_url 窃取 key）；两者都没有（curl/脚本）放行。
"""

from __future__ import annotations

import json
import math
import secrets
import time
from html import escape
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hub.store import Store
from server.tts.edge_tts_client import VOICES

PAGE_SIZE = 50
INT_SETTINGS = {"context_gap_seconds": "上下文间隔（秒）", "context_max_turns": "上下文最大轮数"}
ID_SETTINGS = {
    "current_provider_id": ("当前模型", "providers"),
    "fallback_provider_id": ("退回模型（设备直连兜底）", "providers"),
    "search_provider_id": ("联网搜索模型", "providers"),
    "current_persona_id": ("当前性格", "personas"),
}

CSS = """
body{font-family:sans-serif;margin:16px;max-width:1100px}
nav a{margin-right:12px}
table{border-collapse:collapse;width:100%;margin:8px 0}
td,th{border:1px solid #ccc;padding:4px;vertical-align:top;text-align:left}
input[type=text],textarea,select{width:100%;box-sizing:border-box}
textarea{height:5em}
.err{color:#b00;font-weight:bold}
code{background:#eee;padding:2px 4px}
"""


def _e(value) -> str:
    return escape("" if value is None else str(value))


def mask_key(key: str | None) -> str:
    if not key:
        return "未设置"
    return "****" + key[-4:]


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(title)}</title>"
        f"<style>{CSS}</style></head><body>"
        "<nav><a href='/admin'>总览</a><a href='/admin/providers'>模型供应商</a>"
        "<a href='/admin/personas'>性格</a><a href='/admin/history'>历史</a></nav>"
        f"<h1>{_e(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(html, status_code=status_code)


def _errors_html(errors: list[str]) -> str:
    return "".join(f"<p class='err'>{_e(m)}</p>" for m in errors)


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _options(rows: list[dict], selected: str | None) -> str:
    out = []
    for row in rows:
        sel = " selected" if str(row["id"]) == (selected or "") else ""
        out.append(f"<option value='{row['id']}'{sel}>{_e(row['name'])}</option>")
    return "".join(out)


def _parse_id(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdecimal() else None


def _source_netloc(request: Request) -> str | None:
    """Origin（缺失时 Referer）的 host:port；两者都没有返回 None。"""
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value is not None:
            return urlsplit(value).netloc.lower()  # "null" 等无效值得到 ""
    return None


async def require_same_origin(request: Request) -> None:
    if request.method != "POST":
        return
    source = _source_netloc(request)
    if source is None:
        return
    if source != (request.headers.get("host") or "").lower():
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


def _parse_extra_body(raw: str) -> tuple[str | None, str | None]:
    """返回 (存库文本或 None, 错误信息或 None)。空 → None；必须是 JSON 对象。"""
    raw = raw.strip()
    if not raw:
        return None, None
    try:
        value = json.loads(raw)
    except ValueError:
        return None, "附加请求体不是合法 JSON"
    if not isinstance(value, dict):
        return None, "附加请求体必须是 JSON 对象（{...}）"
    return json.dumps(value, ensure_ascii=False), None


def make_admin_router(store: Store) -> APIRouter:
    router = APIRouter(prefix="/admin", dependencies=[Depends(require_same_origin)])

    def rows_for(table: str) -> list[dict]:
        return store.list_providers() if table == "providers" else store.list_personas()

    # --- 总览 ------------------------------------------------------------

    def overview(errors: list[str] | None = None, status_code: int = 200) -> HTMLResponse:
        parts = [_errors_html(errors or []), "<form method='post' action='/admin'><table>"]
        for key, (label, table) in ID_SETTINGS.items():
            rows = rows_for(table)
            value = store.get_setting(key)
            note = ""
            if rows and not any(str(r["id"]) == (value or "") for r in rows):
                note = f"（原设置已失效，自动使用 {_e(rows[0]['name'])}）"
            elif not rows:
                note = "（暂无可选项）"
            parts.append(
                f"<tr><th>{_e(label)}</th><td><select name='{key}'>"
                f"{_options(rows, value)}</select>{note}</td></tr>"
            )
        for key, label in INT_SETTINGS.items():
            parts.append(
                f"<tr><th>{_e(label)}</th><td><input type='text' name='{key}' "
                f"value='{_e(store.get_setting(key, ''))}'></td></tr>"
            )
        parts.append(
            "<tr><th>基础提示词</th><td><textarea name='base_prompt'>"
            f"{_e(store.get_setting('base_prompt', ''))}</textarea></td></tr>"
            "</table><button type='submit'>保存</button></form>"
            "<h2>设备口令</h2><p>复制到音箱配置（X-Device-Token）：<code>"
            f"{_e(store.get_setting('device_token', ''))}</code></p>"
            "<form method='post' action='/admin'><input type='hidden' name='action' value='regen_token'>"
            "<button type='submit'>重新生成口令</button></form>"
        )
        return _page("NAS 语音助手后台", "".join(parts), status_code)

    @router.get("", response_class=HTMLResponse)
    def admin_home():
        return overview()

    @router.post("")
    async def admin_home_post(request: Request):
        form = await request.form()
        if form.get("action") == "regen_token":
            store.set_setting("device_token", secrets.token_urlsafe(16))
            return _redirect("/admin")
        errors, updates = [], {}
        for key, (label, table) in ID_SETTINGS.items():
            if key not in form:
                continue
            row_id = _parse_id(form.get(key))
            exists = row_id is not None and (
                store.get_provider(row_id) if table == "providers" else store.get_persona(row_id)
            )
            if not exists:
                errors.append(f"{label}（{key}）无效")
            else:
                updates[key] = str(row_id)
        for key, label in INT_SETTINGS.items():
            if key not in form:
                continue
            raw = str(form.get(key)).strip()
            if not raw.isdecimal() or int(raw) <= 0:
                errors.append(f"{label}（{key}）必须是正整数")
            else:
                updates[key] = str(int(raw))
        if "base_prompt" in form:
            updates["base_prompt"] = str(form.get("base_prompt"))
        if errors:
            return overview(errors, 400)
        for key, value in updates.items():
            store.set_setting(key, value)
        return _redirect("/admin")

    # --- 模型供应商 ------------------------------------------------------

    def provider_form(p: dict | None) -> str:
        p = p or {}
        key_hint = f"当前：{_e(mask_key(p.get('api_key')))}，留空表示不修改" if p else ""
        fields = [
            ("name", "名称"), ("aliases", "别名（逗号分隔）"), ("base_url", "Base URL"),
            ("model", "模型"), ("temperature", "temperature（可空）"),
            ("max_tokens", "max_tokens（可空）"),
        ]
        rows = "".join(
            f"<tr><th>{_e(label)}</th><td><input type='text' name='{name}' "
            f"value='{_e(p.get(name))}'></td></tr>"
            for name, label in fields
        )
        rows += (
            "<tr><th>附加请求体 JSON（可空，如 "
            "<code>{&quot;thinking&quot;:{&quot;type&quot;:&quot;disabled&quot;}}</code>）</th>"
            f"<td><textarea name='extra_body'>{_e(p.get('extra_body'))}</textarea></td></tr>"
            "<tr><th>API key</th><td><input type='password' name='api_key' value='' "
            f"autocomplete='off'> {key_hint}</td></tr>"
        )
        return (
            "<form method='post' action='/admin/providers'>"
            "<input type='hidden' name='action' value='save'>"
            f"<input type='hidden' name='id' value='{_e(p.get('id'))}'>"
            f"<table>{rows}</table><button type='submit'>保存</button></form>"
        )

    def delete_form(path: str, row_id: int) -> str:
        return (
            f"<form method='post' action='{path}'><input type='hidden' name='action' value='delete'>"
            f"<input type='hidden' name='id' value='{row_id}'><button type='submit'>删除</button></form>"
        )

    def providers_page(errors: list[str] | None = None, status_code: int = 200) -> HTMLResponse:
        parts = [_errors_html(errors or [])]
        for p in store.list_providers():
            parts.append(
                f"<h3>{_e(p['name'])}（id {p['id']}）</h3>"
                f"<p>模型 {_e(p['model'])} · {_e(p['base_url'])} · key {_e(mask_key(p['api_key']))}</p>"
                f"<details><summary>编辑</summary>{provider_form(p)}</details>"
                f"{delete_form('/admin/providers', p['id'])}"
            )
        parts.append(f"<h2>新增供应商</h2>{provider_form(None)}")
        return _page("模型供应商", "".join(parts), status_code)

    @router.get("/providers", response_class=HTMLResponse)
    def providers_get():
        return providers_page()

    @router.post("/providers")
    async def providers_post(request: Request):
        form = await request.form()
        row_id = _parse_id(form.get("id"))
        if form.get("action") == "delete":
            if row_id is not None:
                store.delete_provider(row_id)
            return _redirect("/admin/providers")
        errors = []
        data = {f: str(form.get(f, "")).strip() for f in ("name", "aliases", "base_url", "model")}
        if not data["name"]:
            errors.append("名称不能为空")
        raw = str(form.get("temperature", "")).strip()
        try:
            temperature = float(raw) if raw else None
        except ValueError:
            temperature = math.nan
        if temperature is not None and not (math.isfinite(temperature) and 0 <= temperature <= 2):
            errors.append("temperature 必须是 0–2 之间的数字")
        data["temperature"] = temperature
        raw = str(form.get("max_tokens", "")).strip()
        try:
            max_tokens = int(raw) if raw else None
        except ValueError:
            max_tokens = 0
        if max_tokens is not None and max_tokens <= 0:
            errors.append("max_tokens 必须是正整数")
        data["max_tokens"] = max_tokens
        if "extra_body" in form:  # 表单没带这个字段（旧脚本）→ 不修改
            data["extra_body"], extra_error = _parse_extra_body(str(form.get("extra_body")))
            if extra_error:
                errors.append(extra_error)
        api_key = str(form.get("api_key", "")).strip()
        if api_key or row_id is None:
            data["api_key"] = api_key  # 编辑时留空 → 不修改
        if errors:
            return providers_page(errors, 400)
        if row_id is not None:
            data["id"] = row_id
        store.upsert_provider(data)
        return _redirect("/admin/providers")

    # --- 性格 ------------------------------------------------------------

    voice_values = list(VOICES.values())

    def persona_form(p: dict | None) -> str:
        p = p or {}
        voice = p.get("voice") or ""
        opts = "".join(
            f"<option value='{_e(v)}'{' selected' if v == voice else ''}>{_e(v)}</option>"
            for v in voice_values
        )
        custom = voice if voice and voice not in voice_values else ""
        return (
            "<form method='post' action='/admin/personas'>"
            "<input type='hidden' name='action' value='save'>"
            f"<input type='hidden' name='id' value='{_e(p.get('id'))}'><table>"
            f"<tr><th>名称</th><td><input type='text' name='name' value='{_e(p.get('name'))}'></td></tr>"
            f"<tr><th>别名（逗号分隔）</th><td><input type='text' name='aliases' value='{_e(p.get('aliases'))}'></td></tr>"
            f"<tr><th>提示词</th><td><textarea name='prompt'>{_e(p.get('prompt'))}</textarea></td></tr>"
            f"<tr><th>音色</th><td><select name='voice'>{opts}</select>"
            f"手填（优先）：<input type='text' name='voice_custom' value='{_e(custom)}'></td></tr>"
            "</table><button type='submit'>保存</button></form>"
        )

    def personas_page(errors: list[str] | None = None, status_code: int = 200) -> HTMLResponse:
        parts = [_errors_html(errors or [])]
        for p in store.list_personas():
            parts.append(
                f"<h3>{_e(p['name'])}（id {p['id']}）</h3><p>音色 {_e(p['voice'])}</p>"
                f"<details><summary>编辑</summary>{persona_form(p)}</details>"
                f"{delete_form('/admin/personas', p['id'])}"
            )
        parts.append(f"<h2>新增性格</h2>{persona_form(None)}")
        return _page("性格", "".join(parts), status_code)

    @router.get("/personas", response_class=HTMLResponse)
    def personas_get():
        return personas_page()

    @router.post("/personas")
    async def personas_post(request: Request):
        form = await request.form()
        row_id = _parse_id(form.get("id"))
        if form.get("action") == "delete":
            if row_id is not None:
                store.delete_persona(row_id)
            return _redirect("/admin/personas")
        data = {f: str(form.get(f, "")).strip() for f in ("name", "aliases", "prompt")}
        data["voice"] = (
            str(form.get("voice_custom", "")).strip() or str(form.get("voice", "")).strip()
        )
        if not data["name"]:
            return personas_page(["名称不能为空"], 400)
        if row_id is not None:
            data["id"] = row_id
        store.upsert_persona(data)
        return _redirect("/admin/personas")

    # --- 历史 ------------------------------------------------------------

    @router.get("/history", response_class=HTMLResponse)
    def history(page: int = 1):
        page = max(page, 1)
        turns = store.list_turns((page - 1) * PAGE_SIZE, PAGE_SIZE + 1)
        has_next = len(turns) > PAGE_SIZE
        rows = "".join(
            "<tr>"
            f"<td>{_e(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t['ts'])))}</td>"
            f"<td>{_e(t['user_text'])}</td><td>{_e(t['answer'])}</td>"
            f"<td>{_e(t['provider_name'])}</td><td>{_e(t['persona_name'])}</td>"
            f"<td>{'是' if t['interrupted'] else ''}</td>"
            f"<td>{'是' if t['web_sources'] else ''}</td>"
            "</tr>"
            for t in turns[:PAGE_SIZE]
        )
        nav = []
        if page > 1:
            nav.append(f"<a href='/admin/history?page={page - 1}'>上一页</a>")
        nav.append(f"第 {page} 页")
        if has_next:
            nav.append(f"<a href='/admin/history?page={page + 1}'>下一页</a>")
        body = (
            "<table><tr><th>时间</th><th>问题</th><th>回答</th><th>模型</th>"
            f"<th>性格</th><th>打断</th><th>联网</th></tr>{rows}</table>"
            f"<p>{' '.join(nav)}</p>"
        )
        return _page("历史记录", body)

    return router
