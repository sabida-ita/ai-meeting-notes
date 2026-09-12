#!/usr/bin/env python3
"""사례조사 대시보드 빌더.

사례조사/ 하위 3개 카테고리 폴더를 재귀 스캔해 마크다운 프론트매터를 파싱하고,
외부 의존성 없는 단일 HTML 파일(dashboard.html)을 생성한다.

사용법:
    python3 사례조사/_index/build_dashboard.py
    python3 사례조사/_index/build_dashboard.py --root <사례조사 경로> --out <출력경로>

노트가 추가될 때마다 재실행하면 dashboard.html이 갱신된다.
표준 라이브러리만 사용한다. pyyaml이 설치되어 있으면 그것을 쓰고, 없으면 내장 파서로 폴백한다.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - 폴백 경로
    yaml = None

CATEGORY_DIRS = [
    ("01_경쟁사_유사서비스", "경쟁사_유사서비스"),
    ("02_니즈검증", "니즈검증"),
    ("03_인접산업", "인접산업"),
]

# 비교 표에 노출할 필드 (키, 표시명)
TABLE_FIELDS = [
    ("target_customer", "타겟 고객"),
    ("pricing", "가격"),
    ("core_offering", "핵심 제공가치"),
    ("strengths", "강점"),
    ("weaknesses", "약점"),
    ("differentiation_gap", "차별화 gap"),
]

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


# --------------------------------------------------------------------------
# 프론트매터 파싱
# --------------------------------------------------------------------------

def _parse_scalar(raw: str) -> str:
    """YAML 스칼라에서 따옴표와 공백을 벗겨낸다."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def _fallback_parse(text: str) -> dict:
    """pyyaml이 없을 때 쓰는 최소 파서.

    지원 형태: `key: value`, `key: [a, b]`, 그리고
        key:
          - item
          - item
    중첩 맵은 지원하지 않는다 (스키마에 없으므로).
    """
    data: dict = {}
    current_key: str | None = None

    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        # 리스트 아이템 (들여쓰기 + "- ")
        if line.startswith((" ", "\t")) and line.lstrip().startswith("- "):
            if current_key is not None:
                item = _parse_scalar(line.lstrip()[2:])
                if item:
                    data.setdefault(current_key, [])
                    if isinstance(data[current_key], list):
                        data[current_key].append(item)
            continue

        if ":" not in line:
            continue

        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        current_key = key

        if rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            data[key] = [_parse_scalar(p) for p in inner.split(",") if p.strip()] if inner else []
        elif rest == "":
            data[key] = []  # 뒤따르는 블록 리스트를 위한 자리. 없으면 빈 리스트로 남는다.
        else:
            data[key] = _parse_scalar(rest)

    return data


def parse_frontmatter(path: Path) -> tuple[dict, str]:
    """(frontmatter dict, body) 를 돌려준다. 프론트매터가 없으면 ({}, 원문)."""
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    raw = match.group(1)
    body = text[match.end():]

    if yaml is not None:
        try:
            data = yaml.safe_load(raw) or {}
            if isinstance(data, dict):
                return data, body
        except Exception as exc:  # 잘못된 YAML은 폴백 파서로 넘긴다
            print(f"  ! YAML 파싱 실패, 내장 파서로 폴백: {path.name} ({exc})", file=sys.stderr)

    return _fallback_parse(raw), body


INLINE_CODE_RE = re.compile(r"`([^`]+)`")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
ITALIC_RE = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
ORDERED_RE = re.compile(r"^(\s*)\d+\.\s+(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def render_inline(text: str) -> str:
    """인라인 마크다운을 HTML로. 이스케이프 후 치환하므로 입력은 원문 그대로 받는다."""
    out = html.escape(text, quote=False)
    out = INLINE_CODE_RE.sub(lambda m: "<code>" + m.group(1) + "</code>", out)
    # 위키링크는 파일이 아니라 대시보드 내부 상세뷰로 연결한다
    out = WIKILINK_RE.sub(
        lambda m: '<a class="xref" href="#" data-note="{0}">{0}</a>'.format(m.group(1)), out
    )
    out = LINK_RE.sub(
        lambda m: '<a href="{}" target="_blank" rel="noopener">{}</a>'.format(m.group(2), m.group(1)),
        out,
    )
    out = BOLD_RE.sub(lambda m: "<strong>" + m.group(1) + "</strong>", out)
    out = ITALIC_RE.sub(lambda m: "<em>" + m.group(1) + "</em>", out)
    return out


def md_to_html(text: str) -> str:
    """노트 본문용 최소 마크다운 렌더러 (표준 라이브러리만 사용).

    지원: 제목, 순서/비순서 목록(중첩), 인용, 굵게/기울임/인라인코드, 링크, 위키링크, 문단.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    parts: list[str] = []
    stack: list[tuple[str, int]] = []   # (tag, indent)
    para: list[str] = []
    quote: list[str] = []

    def close_lists(to_indent: int = -1) -> None:
        while stack and stack[-1][1] > to_indent:
            parts.append("</li></" + stack.pop()[0] + ">")

    def flush_para() -> None:
        if para:
            parts.append("<p>" + render_inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_quote() -> None:
        if quote:
            parts.append("<blockquote>" + render_inline(" ".join(quote)) + "</blockquote>")
            quote.clear()

    for line in lines:
        stripped = line.strip()

        if not stripped:
            flush_para(); flush_quote(); close_lists()
            continue

        heading = HEADING_RE.match(stripped)
        if heading:
            flush_para(); flush_quote(); close_lists()
            level = min(len(heading.group(1)) + 1, 6)   # 노트의 ##는 페이지 h3으로 낮춘다
            parts.append("<h{0}>{1}</h{0}>".format(level, render_inline(heading.group(2))))
            continue

        if stripped.startswith(">"):
            flush_para(); close_lists()
            quote.append(stripped.lstrip("> ").strip())
            continue

        bullet = BULLET_RE.match(line)
        ordered = ORDERED_RE.match(line)
        if bullet or ordered:
            match = bullet or ordered
            tag = "ul" if bullet else "ol"
            indent = len(match.group(1).expandtabs(4))
            flush_para(); flush_quote()

            if stack and indent > stack[-1][1]:
                parts.append("<" + tag + ">")
                stack.append((tag, indent))
            else:
                close_lists(indent)
                if stack and stack[-1][1] == indent:
                    if stack[-1][0] != tag:      # 같은 깊이에서 목록 종류가 바뀌면 교체
                        parts.append("</li></" + stack.pop()[0] + ">")
                        parts.append("<" + tag + ">")
                        stack.append((tag, indent))
                    else:
                        parts.append("</li>")
                else:
                    parts.append("<" + tag + ">")
                    stack.append((tag, indent))
            parts.append("<li>" + render_inline(match.group(2)))
            continue

        # 일반 문단. 목록 안이면 해당 항목의 이어지는 줄로 붙인다.
        if stack:
            parts.append(" " + render_inline(stripped))
        else:
            flush_quote()
            para.append(stripped)

    flush_para(); flush_quote(); close_lists()
    return "".join(parts)


def extract_section(body: str, heading: str) -> str:
    """'## 요약' 같은 섹션의 본문을 뽑아낸다."""
    pattern = re.compile(
        rf"^##\s*{re.escape(heading)}\s*\n(.*?)(?=^##\s|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(body)
    return match.group(1).strip() if match else ""


# --------------------------------------------------------------------------
# 수집
# --------------------------------------------------------------------------

def as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value).strip()


def collect_notes(root: Path) -> list[dict]:
    notes: list[dict] = []

    for dirname, default_category in CATEGORY_DIRS:
        folder = root / dirname
        if not folder.is_dir():
            print(f"  - 폴더 없음, 건너뜀: {dirname}", file=sys.stderr)
            continue

        for path in sorted(folder.rglob("*.md")):
            fm, body = parse_frontmatter(path)
            if not fm:
                print(f"  - 프론트매터 없음, 건너뜀: {path.name}", file=sys.stderr)
                continue

            category = as_text(fm.get("category")) or default_category
            note = {
                "title": as_text(fm.get("title")) or path.stem,
                "category": category,
                "folder": dirname,
                "filename": path.name,
                "relpath": str(path.relative_to(root)),
                "source_url": as_text(fm.get("source_url")),
                "source_type": as_text(fm.get("source_type")),
                "date_collected": as_text(fm.get("date_collected")),
                "confidence": as_text(fm.get("confidence")) or "확인필요",
                "target_customer": as_text(fm.get("target_customer")),
                "pricing": as_text(fm.get("pricing")),
                "core_offering": as_text(fm.get("core_offering")),
                "strengths": as_list(fm.get("strengths")),
                "weaknesses": as_list(fm.get("weaknesses")),
                "data_or_evidence_used": as_text(fm.get("data_or_evidence_used")),
                "relevance_to_us": as_text(fm.get("relevance_to_us")),
                "differentiation_gap": as_text(fm.get("differentiation_gap")),
                "tags": as_list(fm.get("tags")),
                "summary": extract_section(body, "요약"),
                "judgement": extract_section(body, "판단 메모"),
                "body_html": md_to_html(body),
                "slug": path.stem,
            }
            notes.append(note)

    return notes


# --------------------------------------------------------------------------
# 렌더링
# --------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>사례조사 대시보드</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f7f7f5;
    --panel: #ffffff;
    --panel-alt: #fbfbfa;
    --border: #e3e2de;
    --text: #1f1e1c;
    --muted: #6b6963;
    --accent: #3f6d4e;
    --accent-soft: #e6efe8;
    --warn-bg: #fdf1dc;
    --warn-text: #8a5a10;
    --warn-border: #e9cf9d;
    --ok-bg: #e6efe8;
    --ok-text: #2f5c3d;
    --ok-border: #c3dbc9;
    --guess-bg: #ecebfa;
    --guess-text: #4a4590;
    --guess-border: #cfccec;
    --shadow: 0 1px 2px rgba(0,0,0,.05), 0 4px 12px rgba(0,0,0,.04);
  }
  @media (prefers-color-scheme: dark) {
    html[data-theme="auto"] {
      --bg: #191918;
      --panel: #232322;
      --panel-alt: #1f1f1e;
      --border: #353533;
      --text: #eceae5;
      --muted: #9a978f;
      --accent: #8fc3a1;
      --accent-soft: #24352b;
      --warn-bg: #3a2e18;
      --warn-text: #e9c37c;
      --warn-border: #574526;
      --ok-bg: #24352b;
      --ok-text: #9dcfae;
      --ok-border: #35513f;
      --guess-bg: #272641;
      --guess-text: #b4b0ec;
      --guess-border: #3b3966;
      --shadow: 0 1px 2px rgba(0,0,0,.3), 0 4px 12px rgba(0,0,0,.25);
    }
  }
  html[data-theme="dark"] {
    --bg: #191918; --panel: #232322; --panel-alt: #1f1f1e; --border: #353533;
    --text: #eceae5; --muted: #9a978f; --accent: #8fc3a1; --accent-soft: #24352b;
    --warn-bg: #3a2e18; --warn-text: #e9c37c; --warn-border: #574526;
    --ok-bg: #24352b; --ok-text: #9dcfae; --ok-border: #35513f;
    --guess-bg: #272641; --guess-text: #b4b0ec; --guess-border: #3b3966;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 4px 12px rgba(0,0,0,.25);
  }
  html[data-theme="light"] {
    --bg: #f7f7f5; --panel: #ffffff; --panel-alt: #fbfbfa; --border: #e3e2de;
    --text: #1f1e1c; --muted: #6b6963; --accent: #3f6d4e; --accent-soft: #e6efe8;
    --warn-bg: #fdf1dc; --warn-text: #8a5a10; --warn-border: #e9cf9d;
    --ok-bg: #e6efe8; --ok-text: #2f5c3d; --ok-border: #c3dbc9;
    --guess-bg: #ecebfa; --guess-text: #4a4590; --guess-border: #cfccec;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Pretendard",
                 "Noto Sans KR", "Segoe UI", Roboto, sans-serif;
    font-size: 15px;
    line-height: 1.65;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1240px; margin: 0 auto; padding: 28px 20px 80px; }

  header.top { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }
  h1 { font-size: 25px; margin: 0 0 4px; letter-spacing: -.02em; }
  .sub { color: var(--muted); font-size: 13px; margin: 0; }
  .theme-btn {
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 13px; font-size: 13px; cursor: pointer; font-family: inherit;
  }
  .theme-btn:hover { border-color: var(--accent); }

  .stats { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 22px; }
  .stat {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 11px 16px; box-shadow: var(--shadow); min-width: 110px;
  }
  .stat .n { font-size: 21px; font-weight: 650; line-height: 1.2; }
  .stat .l { font-size: 12px; color: var(--muted); }

  .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 18px; }
  .tab {
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    border-radius: 999px; padding: 7px 15px; font-size: 13.5px; cursor: pointer; font-family: inherit;
    transition: background .12s, border-color .12s;
  }
  .tab:hover { border-color: var(--accent); }
  .tab[aria-selected="true"] { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); font-weight: 600; }
  .search {
    flex: 1 1 220px; min-width: 180px; background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; font-size: 13.5px; font-family: inherit;
  }
  .search:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  .toggle-line { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); }

  .badge {
    display: inline-block; font-size: 11.5px; font-weight: 600; padding: 2px 8px;
    border-radius: 999px; border: 1px solid transparent; white-space: nowrap; line-height: 1.6;
  }
  .badge.ok { background: var(--ok-bg); color: var(--ok-text); border-color: var(--ok-border); }
  .badge.warn { background: var(--warn-bg); color: var(--warn-text); border-color: var(--warn-border); }
  .badge.guess { background: var(--guess-bg); color: var(--guess-text); border-color: var(--guess-border); }
  .badge.plain { background: var(--panel-alt); color: var(--muted); border-color: var(--border); }

  .view-title { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 30px 0 10px; }

  .tablewrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 12px; background: var(--panel); box-shadow: var(--shadow); }
  table { border-collapse: collapse; width: 100%; min-width: 1000px; font-size: 13px; }
  th, td { text-align: left; vertical-align: top; padding: 11px 13px; border-bottom: 1px solid var(--border); }
  thead th { background: var(--panel-alt); font-size: 12px; color: var(--muted); font-weight: 600;
             position: sticky; top: 0; z-index: 2; border-bottom: 1px solid var(--border); }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: var(--panel-alt); }
  td.name { min-width: 210px; }
  td.name a { color: var(--text); font-weight: 600; text-decoration: none; }
  td.name a:hover { color: var(--accent); text-decoration: underline; }
  td ul { margin: 0; padding-left: 16px; }
  td li { margin-bottom: 3px; }
  .cellmuted { color: var(--muted); }

  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 14px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px 17px; box-shadow: var(--shadow); }
  .card.flagged { border-left: 3px solid var(--warn-border); }
  .card h3 { font-size: 15.5px; margin: 0 0 7px; line-height: 1.4; }
  .card h3 a { color: var(--text); text-decoration: none; }
  .card h3 a:hover { color: var(--accent); }
  .card .meta { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }
  .card p { margin: 0 0 9px; font-size: 13.5px; }
  .card dt { font-size: 11.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-top: 8px; }
  .card dd { margin: 1px 0 0; font-size: 13px; }
  .card .src { font-size: 12px; margin-top: 11px; }
  .card .src a { color: var(--accent); word-break: break-all; }

  .notelink { background: none; border: none; padding: 0; font: inherit; color: inherit; cursor: pointer; text-align: left; }
  .notelink:hover { color: var(--accent); text-decoration: underline; }

  .overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 50;
    display: flex; justify-content: center; align-items: flex-start; padding: 4vh 16px;
    overflow-y: auto; -webkit-overflow-scrolling: touch;
  }
  .overlay[hidden] { display: none; }
  .sheet {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
    box-shadow: 0 12px 40px rgba(0,0,0,.3); max-width: 780px; width: 100%;
    padding: 26px 30px 34px; margin-bottom: 4vh;
  }
  .sheet-top { display: flex; gap: 12px; align-items: flex-start; justify-content: space-between; margin-bottom: 10px; }
  .sheet h2 { font-size: 20px; margin: 0; line-height: 1.4; letter-spacing: -.01em; }
  .close-btn {
    flex: 0 0 auto; background: var(--panel-alt); border: 1px solid var(--border); color: var(--text);
    border-radius: 8px; width: 32px; height: 32px; font-size: 17px; line-height: 1; cursor: pointer; font-family: inherit;
  }
  .close-btn:hover { border-color: var(--accent); color: var(--accent); }

  .facts { border: 1px solid var(--border); border-radius: 10px; background: var(--panel-alt); padding: 4px 16px; margin: 14px 0 20px; }
  .facts dl { display: grid; grid-template-columns: 120px 1fr; gap: 2px 14px; margin: 12px 0; font-size: 13px; }
  .facts dt { color: var(--muted); font-size: 12px; padding-top: 1px; }
  .facts dd { margin: 0; }
  .facts ul { margin: 0; padding-left: 16px; }

  .prose { font-size: 14.5px; }
  .prose h3 { font-size: 15px; margin: 24px 0 8px; padding-bottom: 5px; border-bottom: 1px solid var(--border); letter-spacing: .01em; }
  .prose h4 { font-size: 14px; margin: 18px 0 6px; }
  .prose p { margin: 0 0 11px; }
  .prose ul, .prose ol { margin: 0 0 11px; padding-left: 20px; }
  .prose li { margin-bottom: 5px; }
  .prose li > ul, .prose li > ol { margin-top: 5px; }
  .prose blockquote {
    margin: 14px 0; padding: 10px 16px; border-left: 3px solid var(--accent);
    background: var(--accent-soft); border-radius: 0 8px 8px 0;
  }
  .prose blockquote p:last-child { margin-bottom: 0; }
  .prose code { background: var(--panel-alt); border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px; font-size: 12.5px; }
  .prose a { color: var(--accent); }
  .prose a.xref { border-bottom: 1px dashed var(--accent); text-decoration: none; }

  .empty { padding: 40px; text-align: center; color: var(--muted); }
  footer { margin-top: 40px; font-size: 12px; color: var(--muted); border-top: 1px solid var(--border); padding-top: 14px; }

  @media (max-width: 640px) {
    .wrap { padding: 20px 14px 60px; }
    h1 { font-size: 21px; }
    .cards { grid-template-columns: 1fr; }
    .overlay { padding: 0; }
    .sheet { border-radius: 0; border: none; padding: 20px 16px 40px; margin-bottom: 0; min-height: 100%; }
    .facts dl { grid-template-columns: 1fr; gap: 0 0; }
    .facts dt { margin-top: 8px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>사례조사 대시보드</h1>
      <p class="sub">골프 수강생 유형 분류 &amp; 티칭 가이드 서비스 — 경쟁사 / 니즈검증 / 인접산업</p>
    </div>
    <button class="theme-btn" id="themeBtn" type="button">테마: 자동</button>
  </header>

  <div class="stats" id="stats"></div>

  <div class="controls">
    <div id="tabs" role="tablist" style="display:flex;flex-wrap:wrap;gap:8px;"></div>
    <input class="search" id="search" type="search" placeholder="제목 · 태그 · 내용 검색" aria-label="검색">
    <label class="toggle-line"><input type="checkbox" id="onlyFlag"> 확인필요만</label>
  </div>

  <h2 class="view-title">사례 비교 표</h2>
  <div class="tablewrap"><table id="cmp"><thead></thead><tbody></tbody></table></div>

  <h2 class="view-title">사례 카드</h2>
  <div class="cards" id="cards"></div>

  <footer id="foot"></footer>
</div>

<div class="overlay" id="overlay" hidden role="dialog" aria-modal="true" aria-labelledby="sheetTitle">
  <article class="sheet" id="sheet"></article>
</div>

<script>
const NOTES = __DATA__;
const FIELDS = __FIELDS__;
const GENERATED = "__GENERATED__";
const CATS = ["전체", "경쟁사_유사서비스", "니즈검증", "인접산업"];

let activeCat = "전체";
let query = "";
let onlyFlag = false;

const esc = s => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function badgeClass(conf) {
  if (conf === "확인됨") return "ok";
  if (conf === "확인필요") return "warn";
  if (conf === "추정") return "guess";
  return "plain";
}

function matches(n) {
  if (activeCat !== "전체" && n.category !== activeCat) return false;
  if (onlyFlag && n.confidence !== "확인필요") return false;
  if (!query) return true;
  const hay = [n.title, n.category, n.tags.join(" "), n.summary, n.core_offering,
               n.target_customer, n.pricing, n.relevance_to_us, n.differentiation_gap,
               n.strengths.join(" "), n.weaknesses.join(" "), n.judgement].join(" ").toLowerCase();
  return hay.includes(query);
}

function cell(n, key) {
  const v = n[key];
  if (Array.isArray(v)) {
    if (!v.length) return '<span class="cellmuted">—</span>';
    return "<ul>" + v.map(x => "<li>" + esc(x) + "</li>").join("") + "</ul>";
  }
  return v ? esc(v) : '<span class="cellmuted">—</span>';
}

function render() {
  const rows = NOTES.filter(matches);

  // 통계
  const byCat = {};
  NOTES.forEach(n => { byCat[n.category] = (byCat[n.category] || 0) + 1; });
  const flagged = NOTES.filter(n => n.confidence === "확인필요").length;
  const statHtml = [
    ['<div class="n">' + NOTES.length + '</div><div class="l">전체 노트</div>'],
    ...CATS.slice(1).map(c => ['<div class="n">' + (byCat[c] || 0) + '</div><div class="l">' + esc(c) + '</div>']),
    ['<div class="n">' + flagged + '</div><div class="l">확인필요</div>'],
  ].map(x => '<div class="stat">' + x + '</div>').join("");
  document.getElementById("stats").innerHTML = statHtml;

  // 탭
  document.getElementById("tabs").innerHTML = CATS.map(c => {
    const cnt = c === "전체" ? NOTES.length : (byCat[c] || 0);
    return '<button class="tab" role="tab" data-cat="' + esc(c) + '" aria-selected="' +
      (c === activeCat) + '">' + esc(c) + ' <span class="cellmuted">' + cnt + '</span></button>';
  }).join("");
  document.querySelectorAll(".tab").forEach(b => {
    b.onclick = () => { activeCat = b.dataset.cat; render(); };
  });

  // 표
  const thead = document.querySelector("#cmp thead");
  const tbody = document.querySelector("#cmp tbody");
  thead.innerHTML = "<tr><th>사례</th><th>신뢰도</th>" +
    FIELDS.map(f => "<th>" + esc(f[1]) + "</th>").join("") + "</tr>";
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="' + (FIELDS.length + 2) + '" class="empty">조건에 맞는 사례가 없습니다.</td></tr>';
  } else {
    tbody.innerHTML = rows.map(n => {
      const link = '<button class="notelink" data-note="' + esc(n.slug) + '">' + esc(n.title) + "</button>";
      return "<tr><td class=\\"name\\">" + link +
        '<div class="cellmuted" style="font-size:11.5px;margin-top:3px;">' + esc(n.category) +
        (n.source_type ? " · " + esc(n.source_type) : "") + "</div></td>" +
        '<td><span class="badge ' + badgeClass(n.confidence) + '">' + esc(n.confidence) + "</span></td>" +
        FIELDS.map(f => "<td>" + cell(n, f[0]) + "</td>").join("") + "</tr>";
    }).join("");
  }

  // 카드
  const cards = document.getElementById("cards");
  if (!rows.length) {
    cards.innerHTML = '<div class="empty">조건에 맞는 사례가 없습니다.</div>';
  } else {
    cards.innerHTML = rows.map(n => {
      const tags = n.tags.slice(0, 6).map(t => '<span class="badge plain">' + esc(t) + "</span>").join("");
      return '<article class="card' + (n.confidence === "확인필요" ? " flagged" : "") + '">' +
        '<h3><button class="notelink" data-note="' + esc(n.slug) + '">' + esc(n.title) + "</button></h3>" +
        '<div class="meta"><span class="badge ' + badgeClass(n.confidence) + '">' + esc(n.confidence) + "</span>" +
        '<span class="badge plain">' + esc(n.category) + "</span>" +
        (n.date_collected ? '<span class="badge plain">' + esc(n.date_collected) + "</span>" : "") + "</div>" +
        (n.summary ? "<p>" + esc(n.summary) + "</p>" : "") +
        (n.relevance_to_us ? "<dl><dt>우리 가설과의 관련</dt><dd>" + esc(n.relevance_to_us) + "</dd></dl>" : "") +
        (n.differentiation_gap ? "<dl><dt>차별화 gap</dt><dd>" + esc(n.differentiation_gap) + "</dd></dl>" : "") +
        (tags ? '<div class="meta" style="margin-top:10px">' + tags + "</div>" : "") +
        '<div class="src">' + (n.source_url
            ? '출처: <a href="' + esc(n.source_url) + '" target="_blank" rel="noopener">' + esc(n.source_url) + "</a>"
            : '<span class="cellmuted">출처 URL 없음</span>') + "</div>" +
        "</article>";
    }).join("");
  }

  document.getElementById("foot").textContent =
    "생성 시각 " + GENERATED + " · 표시 중 " + rows.length + " / 전체 " + NOTES.length +
    "건 · 제목을 클릭하면 노트 전문을 볼 수 있습니다 · 노트를 추가한 뒤 build_dashboard.py 를 다시 실행하면 갱신됩니다.";
}

// ---------- 노트 상세뷰 ----------
const overlay = document.getElementById("overlay");
const sheet = document.getElementById("sheet");
const bySlug = {};
NOTES.forEach(n => { bySlug[n.slug] = n; });
let lastFocus = null;

function factRow(label, value) {
  if (!value || (Array.isArray(value) && !value.length)) return "";
  const body = Array.isArray(value)
    ? "<ul>" + value.map(x => "<li>" + esc(x) + "</li>").join("") + "</ul>"
    : esc(value);
  return "<dt>" + esc(label) + "</dt><dd>" + body + "</dd>";
}

function openNote(slug) {
  const n = bySlug[slug];
  if (!n) return;
  lastFocus = document.activeElement;

  const facts = [
    factRow("타겟 고객", n.target_customer),
    factRow("가격", n.pricing),
    factRow("핵심 제공가치", n.core_offering),
    factRow("강점", n.strengths),
    factRow("약점", n.weaknesses),
    factRow("근거 자료", n.data_or_evidence_used),
    factRow("우리 가설과의 관련", n.relevance_to_us),
    factRow("차별화 gap", n.differentiation_gap),
  ].join("");

  sheet.innerHTML =
    '<div class="sheet-top"><h2 id="sheetTitle">' + esc(n.title) + "</h2>" +
    '<button class="close-btn" id="closeBtn" aria-label="닫기">&times;</button></div>' +
    '<div class="meta"><span class="badge ' + badgeClass(n.confidence) + '">' + esc(n.confidence) + "</span>" +
    '<span class="badge plain">' + esc(n.category) + "</span>" +
    (n.source_type ? '<span class="badge plain">' + esc(n.source_type) + "</span>" : "") +
    (n.date_collected ? '<span class="badge plain">' + esc(n.date_collected) + "</span>" : "") +
    (n.tags || []).map(t => '<span class="badge plain">' + esc(t) + "</span>").join("") + "</div>" +
    (n.source_url
      ? '<p style="font-size:12.5px;margin:10px 0 0">출처: <a href="' + esc(n.source_url) +
        '" target="_blank" rel="noopener" style="color:var(--accent);word-break:break-all">' + esc(n.source_url) + "</a></p>"
      : '<p style="font-size:12.5px;margin:10px 0 0" class="cellmuted">출처 URL 없음</p>') +
    (facts ? '<div class="facts"><dl>' + facts + "</dl></div>" : "") +
    '<div class="prose">' + n.body_html + "</div>" +
    '<p style="font-size:12px;margin-top:24px" class="cellmuted">원본 파일: ' + esc(n.relpath) + "</p>";

  overlay.hidden = false;
  document.body.style.overflow = "hidden";
  overlay.scrollTop = 0;
  document.getElementById("closeBtn").onclick = closeNote;
  document.getElementById("closeBtn").focus();
}

function closeNote() {
  overlay.hidden = true;
  document.body.style.overflow = "";
  if (lastFocus) lastFocus.focus();
}

// 노트 제목 클릭, 그리고 본문 안의 [[위키링크]] 클릭 모두 처리
document.addEventListener("click", e => {
  const trigger = e.target.closest("[data-note]");
  if (trigger) {
    e.preventDefault();
    const slug = trigger.dataset.note;
    if (bySlug[slug]) {
      openNote(slug);
    } else {
      alert("연결된 노트를 찾을 수 없습니다: " + slug);
    }
    return;
  }
  if (e.target === overlay) closeNote();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !overlay.hidden) closeNote();
});

document.getElementById("search").addEventListener("input", e => {
  query = e.target.value.trim().toLowerCase();
  render();
});
document.getElementById("onlyFlag").addEventListener("change", e => {
  onlyFlag = e.target.checked;
  render();
});

const themes = ["auto", "light", "dark"];
const labels = { auto: "자동", light: "라이트", dark: "다크" };
const btn = document.getElementById("themeBtn");
btn.addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme || "auto";
  const next = themes[(themes.indexOf(cur) + 1) % themes.length];
  document.documentElement.dataset.theme = next;
  btn.textContent = "테마: " + labels[next];
});

render();
</script>
</body>
</html>
"""


def render_html(notes: list[dict]) -> str:
    payload = json.dumps(notes, ensure_ascii=False).replace("</", "<\\/")
    fields = json.dumps(TABLE_FIELDS, ensure_ascii=False).replace("</", "<\\/")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        HTML_TEMPLATE
        .replace("__DATA__", payload)
        .replace("__FIELDS__", fields)
        .replace("__GENERATED__", generated)
    )


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="사례조사 대시보드를 생성한다.")
    parser.add_argument("--root", default=str(here.parent), help="사례조사 폴더 경로 (기본: 이 스크립트의 상위 폴더)")
    parser.add_argument("--out", default=str(here / "dashboard.html"), help="출력 HTML 경로")
    parser.add_argument(
        "--deploy-out",
        default=str(here.parent.parent / "research" / "index.html"),
        help="배포용 사본 경로 (Vercel이 정적으로 서빙. 빈 문자열이면 생성하지 않음)",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"오류: 폴더를 찾을 수 없습니다 — {root}", file=sys.stderr)
        return 1

    print(f"스캔: {root}")
    notes = collect_notes(root)
    if not notes:
        print("경고: 파싱된 노트가 없습니다. 빈 대시보드를 생성합니다.", file=sys.stderr)

    document = render_html(notes)

    targets = [Path(args.out).resolve()]
    if args.deploy_out:
        targets.append(Path(args.deploy_out).resolve())

    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document, encoding="utf-8")

    out = targets[0]

    flagged = [n for n in notes if n["confidence"] == "확인필요"]
    print(f"노트 {len(notes)}건 파싱 (파서: {'pyyaml' if yaml else '내장'})")
    for _, cat in CATEGORY_DIRS:
        print(f"  · {cat}: {sum(1 for n in notes if n['category'] == cat)}건")
    print(f"confidence=확인필요: {len(flagged)}건")
    for n in flagged:
        print(f"  ! {n['title']}")
    for target in targets:
        print(f"생성 완료 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
