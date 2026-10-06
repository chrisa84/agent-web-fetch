"""Content extraction and retrieval-failure detection.

Browser pages are converted to Markdown inside the page with plain DOM APIs
(EXTRACT_JS), so no HTML parsing library is needed. Jina already returns
Markdown; both paths go through the same cleanup and failure heuristics.
"""

import re
from typing import Optional

MAX_CONTENT_CHARS = 400_000

# Runs in the rendered page. Marks hidden elements on the live DOM (computed
# style is only available there), clones the best content root, strips junk
# from the clone, then walks it into Markdown.
EXTRACT_JS = r"""
() => {
  const HIDDEN = 'data-awf-hidden';
  for (const el of document.body ? document.body.querySelectorAll('*') : []) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || el.hidden ||
        el.getAttribute('aria-hidden') === 'true') el.setAttribute(HIDDEN, '1');
  }
  const textLen = el => (el.innerText || '').trim().length;
  let root = null;
  const candidates = [...document.querySelectorAll('main, [role=main], article')]
    .filter(el => !el.hasAttribute(HIDDEN));
  for (const c of candidates) if (!root || textLen(c) > textLen(root)) root = c;
  const bodyLen = document.body ? textLen(document.body) : 0;
  if (!root || textLen(root) < bodyLen * 0.25) root = document.body;
  if (!root) return {title: document.title || '', markdown: ''};
  const clone = root.cloneNode(true);
  for (const el of document.querySelectorAll('[' + HIDDEN + ']')) el.removeAttribute(HIDDEN);

  const JUNK = 'script, style, noscript, template, iframe, object, embed, svg, canvas, video, audio, ' +
    'form, button, select, input, textarea, dialog, nav, aside, footer, ' +
    '[role=navigation], [role=banner], [role=contentinfo], [role=complementary], [role=dialog], ' +
    '[role=alertdialog], [role=search], [' + HIDDEN + ']';
  for (const el of clone.querySelectorAll(JUNK)) el.remove();
  if (root === document.body) for (const el of clone.querySelectorAll('header')) el.remove();
  const JUNK_NAME = /(^|[\s_-])(cookie|consent|gdpr|banner-ad|advert|advertisement|ads|ad-slot|sponsor|promo|newsletter|social-share|share-buttons|breadcrumbs?|skip-link)([\s_-]|$)/i;
  for (const el of clone.querySelectorAll('[class], [id]')) {
    const name = (el.getAttribute('class') || '') + ' ' + (el.id || '');
    if (JUNK_NAME.test(name) && (el.innerText || el.textContent || '').length < 3000) el.remove();
  }

  const abs = href => { try { return new URL(href, document.baseURI).href; } catch (e) { return ''; } };
  const esc = s => s.replace(/\s+/g, ' ');
  function inline(node) {
    let out = '';
    for (const n of node.childNodes) {
      if (n.nodeType === 3) { out += esc(n.textContent); continue; }
      if (n.nodeType !== 1) continue;
      const tag = n.tagName.toLowerCase();
      if (tag === 'br') out += '\n';
      else if (tag === 'a') {
        const t = inline(n).trim(); const h = abs(n.getAttribute('href') || '');
        out += (t && h && /^https?:/.test(h)) ? `[${t}](${h})` : t;
      } else if (tag === 'strong' || tag === 'b') { const t = inline(n).trim(); out += t ? `**${t}**` : ''; }
      else if (tag === 'em' || tag === 'i') { const t = inline(n).trim(); out += t ? `*${t}*` : ''; }
      else if (tag === 'code') out += '`' + n.textContent + '`';
      else if (tag === 'img') { const a = (n.getAttribute('alt') || '').trim(); if (a) out += `[image: ${esc(a)}]`; }
      else if (BLOCKS.has(tag)) out += '\n' + block(n) + '\n';
      else out += inline(n);
    }
    return out;
  }
  const BLOCKS = new Set(['p','div','section','article','main','header','h1','h2','h3','h4','h5','h6','ul','ol',
    'li','pre','blockquote','table','hr','figure','figcaption','dl','dt','dd','details','summary']);
  function list(n, depth) {
    const ordered = n.tagName.toLowerCase() === 'ol'; let i = 1; const lines = [];
    for (const li of n.children) {
      if (li.tagName.toLowerCase() !== 'li') continue;
      let text = ''; const nested = [];
      for (const c of li.childNodes) {
        if (c.nodeType === 1 && ['ul','ol'].includes(c.tagName.toLowerCase())) nested.push(list(c, depth + 1));
        else if (c.nodeType === 1) text += BLOCKS.has(c.tagName.toLowerCase()) ? ' ' + block(c) : inline({childNodes: [c]});
        else if (c.nodeType === 3) text += esc(c.textContent);
      }
      lines.push('  '.repeat(depth) + (ordered ? `${i++}. ` : '- ') + text.trim().replace(/\n+/g, ' '));
      lines.push(...nested.filter(Boolean));
    }
    return lines.join('\n');
  }
  function table(n) {
    // Layout tables (tables containing tables) are rendered as plain containers.
    if (n.querySelector('table')) return [...n.querySelectorAll('td, th')].filter(td => td.closest('table') === n)
      .map(td => children(td)).filter(t => t.trim()).join('\n\n');
    const rows = [...n.querySelectorAll('tr')].filter(tr => tr.closest('table') === n).map(tr =>
      [...tr.children].map(td => inline(td).trim().replace(/\|/g, '\\|').replace(/\n+/g, ' ')));
    if (!rows.length) return '';
    const w = Math.max(...rows.map(r => r.length));
    const fmt = r => '| ' + [...r, ...Array(w - r.length).fill('')].join(' | ') + ' |';
    return [fmt(rows[0]), '| ' + Array(w).fill('---').join(' | ') + ' |', ...rows.slice(1).map(fmt)].join('\n');
  }
  function block(n) {
    const tag = n.tagName.toLowerCase();
    if (/^h[1-6]$/.test(tag)) { const t = inline(n).trim().replace(/\n+/g, ' '); return t ? '#'.repeat(+tag[1]) + ' ' + t : ''; }
    if (tag === 'ul' || tag === 'ol') return list(n, 0);
    if (tag === 'pre') {
      const code = n.querySelector('code'); const m = ((code && code.className) || '').match(/language-([\w+-]+)/);
      return '```' + (m ? m[1] : '') + '\n' + n.textContent.replace(/\n+$/, '') + '\n```';
    }
    if (tag === 'blockquote') return children(n).split('\n').map(l => '> ' + l).join('\n');
    if (tag === 'table') return table(n);
    if (tag === 'hr') return '---';
    return children(n);
  }
  function children(n) {
    const parts = []; let buf = '';
    const flush = () => { if (buf.trim()) parts.push(buf.trim()); buf = ''; };
    for (const c of n.childNodes) {
      if (c.nodeType === 1 && BLOCKS.has(c.tagName.toLowerCase())) { flush(); const b = block(c); if (b.trim()) parts.push(b); }
      else if (c.nodeType === 1 || c.nodeType === 3) buf += inline({childNodes: [c]});
    }
    flush();
    return parts.join('\n\n');
  }
  return {title: document.title || '', markdown: children(clone)};
}
"""

BOUNDARY_BEGIN = "BEGIN UNTRUSTED WEB CONTENT"
BOUNDARY_END = "END UNTRUSTED WEB CONTENT"


def clean_markdown(text: str) -> str:
    """Normalise whitespace, drop boundary-marker spoofs, and cap length."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(re.escape(BOUNDARY_BEGIN) + "|" + re.escape(BOUNDARY_END), "[boundary marker removed]", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + "\n\n[content truncated]"
    return text


def clean_title(title: Optional[str]) -> str:
    return re.sub(r"\s+", " ", title or "").strip()[:300]


# --- failure detection -------------------------------------------------------
# Deliberately small. Strong phrases only count on short pages, so an article
# that merely discusses CAPTCHAs is not treated as a challenge page.

CHALLENGE_TITLES = re.compile(
    r"^(just a moment|attention required|access denied|403 forbidden|forbidden|"
    r"are you a robot|security check|human verification|verify you are human|ddos-guard|"
    r"please wait|one more step|robot check|captcha)\b",
    re.IGNORECASE,
)
CHALLENGE_PHRASES = re.compile(
    r"verify (that )?you are (a )?human|checking (if the site connection is secure|your browser)|"
    r"enable javascript and cookies to continue|are you a robot|unusual traffic from your computer|"
    r"\bcaptcha\b|cf-chl|ray id:|access (to this page has been )?denied|request blocked|"
    r"you have been blocked|bot (detection|protection|verification)|press (and|&) hold",
    re.IGNORECASE,
)
JS_REQUIRED = re.compile(
    r"(you need to |please )?enable javascript|javascript is (disabled|required)|"
    r"requires javascript|this (app|site) works best with javascript",
    re.IGNORECASE,
)
LOGIN_PHRASES = re.compile(
    r"(sign|log) ?in to (continue|view|see|access)|you must be (logged|signed) in|"
    r"please (sign|log) ?in|authentication required|login required",
    re.IGNORECASE,
)
LOGIN_PATH = re.compile(r"/(login|log-in|signin|sign-in|sso|auth|oauth|account/login)(/|$|\?)", re.IGNORECASE)

MIN_CONTENT_CHARS = 120
SHORT_PAGE_CHARS = 2500


def detect_failure(title: str, content: str, status: Optional[int] = None,
                   url: str = "", final_url: str = "",
                   min_chars: int = MIN_CONTENT_CHARS) -> Optional[dict]:
    """Return {"category", "message"} when the result is obviously not the page, else None."""
    if status in (401, 407):
        return {"category": "auth_required", "message": "authentication required"}
    if status == 403:
        return {"category": "blocked_by_site", "message": "access forbidden (HTTP 403)"}
    if status == 429:
        return {"category": "rate_limited", "message": "site is rate limiting requests (HTTP 429)"}
    if status in (404, 410):
        return {"category": "not_found", "message": f"page not found (HTTP {status})"}
    if status is not None and status >= 400:
        return {"category": "http_error", "message": f"HTTP {status}"}

    title = title or ""
    body = content or ""
    if CHALLENGE_TITLES.search(title.strip()):
        return {"category": "blocked_by_site", "message": "bot challenge or access-denied page"}
    if final_url and LOGIN_PATH.search(final_url) and not LOGIN_PATH.search(url):
        return {"category": "auth_required", "message": "authentication required"}

    visible = re.sub(r"\[[^\]]*\]\([^)]*\)|[#>*`|\-_]", " ", body)
    visible_len = len(re.sub(r"\s+", " ", visible).strip())
    if visible_len < SHORT_PAGE_CHARS:
        if CHALLENGE_PHRASES.search(body):
            return {"category": "blocked_by_site", "message": "bot challenge or access-denied page"}
        if LOGIN_PHRASES.search(body) and visible_len < 1000:
            return {"category": "auth_required", "message": "authentication required"}
        if JS_REQUIRED.search(body) and visible_len < 1000:
            return {"category": "js_required", "message": "page is a JavaScript-required shell"}
    if visible_len < min_chars:
        return {"category": "empty_content", "message": f"page content is nearly empty ({visible_len} chars)"}
    return None
