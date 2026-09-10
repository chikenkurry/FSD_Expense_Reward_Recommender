"""Dependency-free HTTP API with explicit boundary validation and no secret logging.

Implements public catalogue discovery endpoints, internal recommendation snapshots,
admin operations for scraping and review workflows, and a built-in testing UI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import db
from .config import ConfigError, selected_sources
from .scraper import scrape_sources
from .util import utc_now

MAX_BODY = 64_000


def _uuid(v: object) -> str | None:
    """Validate and return canonical UUID string, or None if invalid."""
    try:
        val = str(v)
        return val if str(uuid.UUID(val)) == val else None
    except (ValueError, TypeError, AttributeError):
        return None


UUID = _uuid


def _json(value: object) -> bytes:
    """Serialize value to compact, sorted JSON bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _etag(revision: int, representation: str) -> str:
    """Generate a deterministic strong ETag for a given catalogue revision and query representation."""
    hashed = hashlib.sha256(representation.encode()).hexdigest()[:12]
    return f'"catalogue-{revision}-{hashed}"'


def _cursor(secret: str, filters: dict, revision: int, offset: int) -> str:
    """Create an HMAC-SHA256 authenticated pagination cursor with 15-minute expiry."""
    payload = {
        "f": filters,
        "r": revision,
        "o": offset,
        "e": int(time.time()) + 900,
    }
    raw = _json(payload)
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + b"." + sig).decode().rstrip("=")


def _decode_cursor(secret: str, token: str, filters: dict, revision: int) -> int:
    """Verify HMAC signature and freshness of cursor, returning item offset."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        body, sig = raw.rsplit(b".", 1)
        expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected_sig):
            raise ValueError
        parsed = json.loads(body)
        if parsed["f"] != filters:
            raise LookupError("CURSOR_FILTER_MISMATCH")
        if parsed["e"] < time.time() or parsed["r"] != revision:
            raise RuntimeError("CURSOR_EXPIRED")
        return int(parsed["o"])
    except (ValueError, KeyError, json.JSONDecodeError, TypeError):
        raise ValueError("VALIDATION_ERROR")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Credit Card Rewards Catalogue</title>
<style>
  :root {
    --bg: #0f172a; --surface: #1e293b; --surface-hover: #334155;
    --border: #334155; --text: #f8fafc; --muted: #94a3b8;
    --primary: #38bdf8; --primary-hover: #0284c7; --accent: #10b981;
    --warning: #f59e0b; --danger: #ef4444; --card-bg: #1e293b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5; padding: 24px;
  }
  header {
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px;
    flex-wrap: wrap; gap: 12px;
  }
  h1 { font-size: 1.5rem; font-weight: 700; color: var(--primary); display: flex; align-items: center; gap: 8px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 9999px;
    font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
  }
  .badge-cashback { background: #065f46; color: #34d399; }
  .badge-miles { background: #1e3a8a; color: #60a5fa; }
  .badge-points { background: #701a75; color: #f472b6; }
  .badge-pending { background: #78350f; color: #fbbf24; }
  .badge-approved { background: #065f46; color: #34d399; }
  .badge-rejected { background: #7f1d1d; color: #f87171; }
  nav { display: flex; gap: 8px; }
  button, .btn {
    background: var(--surface); color: var(--text); border: 1px solid var(--border);
    padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 0.875rem;
    font-weight: 500; transition: all 0.15s;
  }
  button:hover, .btn:hover { background: var(--surface-hover); border-color: var(--primary); }
  button.active { background: var(--primary); color: #000; border-color: var(--primary); }
  button.btn-primary { background: var(--primary); color: #000; border: none; font-weight: 600; }
  button.btn-primary:hover { background: var(--primary-hover); }
  button.btn-danger { background: var(--danger); color: #fff; border: none; }
  button.btn-success { background: var(--accent); color: #fff; border: none; }
  .controls {
    display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; align-items: center;
  }
  input, select {
    background: var(--surface); color: var(--text); border: 1px solid var(--border);
    padding: 8px 12px; border-radius: 6px; font-size: 0.875rem;
  }
  input:focus, select:focus { outline: none; border-color: var(--primary); }
  .cards-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px;
  }
  .card-item {
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 16px; display: flex; flex-direction: column; justify-content: space-between;
    transition: transform 0.1s, border-color 0.1s;
  }
  .card-item:hover { transform: translateY(-2px); border-color: var(--primary); }
  .card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }
  .card-title { font-size: 1.1rem; font-weight: 600; color: #fff; }
  .card-issuer { font-size: 0.85rem; color: var(--muted); }
  .card-headline { font-size: 0.95rem; color: var(--accent); margin: 8px 0; font-weight: 500; }
  .card-meta { font-size: 0.8rem; color: var(--muted); margin-bottom: 12px; display: flex; gap: 12px; }
  .card-actions { display: flex; gap: 8px; border-top: 1px solid var(--border); padding-top: 12px; }
  .modal {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7);
    align-items: center; justify-content: center; padding: 20px; z-index: 100;
  }
  .modal.open { display: flex; }
  .modal-content {
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    max-width: 700px; width: 100%; max-height: 85vh; overflow-y: auto; padding: 24px;
    box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5);
  }
  .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  .close-btn { background: none; border: none; font-size: 1.5rem; color: var(--muted); cursor: pointer; }
  pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px; font-size: 0.8rem; overflow-x: auto; color: #cbd5e1; margin-top: 12px;
  }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; }
  th, td { border: 1px solid var(--border); padding: 8px 12px; text-align: left; font-size: 0.85rem; }
  th { background: var(--bg); color: var(--muted); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .diff-add { color: var(--accent); }
  .diff-replace { color: var(--warning); }
  .diff-remove { color: var(--danger); }
</style>
</head>
<body>

<header>
  <h1>Credit Card Rewards Catalogue</h1>
  <nav>
    <button id="tabCardsBtn" class="active" onclick="showTab('tabCards')">Catalogue Cards</button>
    <button id="tabCompareBtn" onclick="showTab('tabCompare')">Comparison Tool</button>
    <button id="tabAdminBtn" onclick="showTab('tabAdmin')">Review Dashboard</button>
  </nav>
</header>

<!-- Tab 1: Catalogue Cards -->
<section id="tabCards" class="tab-content active">
  <div class="controls">
    <input type="text" id="searchInput" placeholder="Search by card or bank..." oninput="debounceLoadCards()">
    <select id="rewardTypeSelect" onchange="loadCards()">
      <option value="">All Reward Types</option>
      <option value="cashback">Cashback</option>
      <option value="miles">Air Miles</option>
      <option value="points">Points</option>
      <option value="mixed">Mixed</option>
    </select>
    <select id="supportSelect" onchange="loadCards()">
      <option value="">All Simulation Support</option>
      <option value="supported">Supported</option>
      <option value="unsupported">Unsupported</option>
    </select>
    <button onclick="loadCards()">Refresh</button>
    <span id="catalogueRevisionBadge" style="margin-left:auto; font-size:0.85rem; color:var(--muted)"></span>
  </div>
  <div id="cardsList" class="cards-grid"></div>
</section>

<!-- Tab 2: Comparison Tool -->
<section id="tabCompare" class="tab-content">
  <div class="controls">
    <p style="font-size:0.9rem; color:var(--muted)">Select 2 to 4 cards to compare reward rates, fee waivers, caps, and eligibility side-by-side.</p>
    <button class="btn-primary" onclick="runComparison()">Compare Selected (<span id="compareCount">0</span>)</button>
  </div>
  <div id="compareSelectGrid" class="cards-grid" style="margin-bottom:24px;"></div>
  <div id="compareResults"></div>
</section>

<!-- Tab 3: Admin Review Dashboard & Crawler Controls -->
<section id="tabAdmin" class="tab-content">
  <div class="controls" style="background:var(--surface); padding:16px; border-radius:8px; border:1px solid var(--border); margin-bottom:20px;">
    <div style="width:100%; display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
      <label><strong>Admin Token:</strong></label>
      <input type="password" id="adminSecretInput" placeholder="Enter CARD_ADMIN_BEARER token..." style="min-width:280px;" oninput="onAdminSecretChange()">
      <span style="font-size:0.8rem; color:var(--muted);">(Required for scraping, crawling, and reviews)</span>
    </div>
  </div>

  <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:20px; margin-bottom:24px;">
    <!-- Box 1: Trigger Scraper for Configured Sources -->
    <div class="card-item">
      <h3 style="margin-bottom:8px; font-size:1.1rem; color:var(--primary);">Trigger Configured Scraper</h3>
      <p style="font-size:0.85rem; color:var(--muted); margin-bottom:12px;">Run the scraper on pre-configured bank sources or directories.</p>
      <div style="display:flex; flex-direction:column; gap:10px;">
        <select id="configSourceSelect">
          <option value="">All Configured Sources</option>
        </select>
        <button class="btn-primary" onclick="triggerScrapeFromUi()">Run Scraper Now</button>
      </div>
      <div id="scrapeFeedback" style="margin-top:12px; font-size:0.85rem;"></div>
    </div>

    <!-- Box 2: Live Bank Directory Crawler -->
    <div class="card-item">
      <h3 style="margin-bottom:8px; font-size:1.1rem; color:var(--accent);">Live Bank Directory Crawler</h3>
      <p style="font-size:0.85rem; color:var(--muted); margin-bottom:8px;">Crawl a bank overview page to automatically discover and extract cards.</p>
      <div style="display:flex; gap:6px; margin-bottom:10px; flex-wrap:wrap; align-items:center;">
        <span style="font-size:0.8rem; color:var(--muted);">Presets:</span>
        <button type="button" style="padding:3px 8px; font-size:0.75rem; border-radius:4px; border:1px solid var(--border); background:var(--surface);" onclick="setBankPreset('dbs')">DBS Hub</button>
        <button type="button" style="padding:3px 8px; font-size:0.75rem; border-radius:4px; border:1px solid var(--border); background:var(--surface);" onclick="setBankPreset('ocbc')">OCBC Hub</button>
        <button type="button" style="padding:3px 8px; font-size:0.75rem; border-radius:4px; border:1px solid var(--border); background:var(--surface);" onclick="setBankPreset('uob')">UOB Hub</button>
      </div>
      <div style="display:flex; flex-direction:column; gap:10px;">
        <input type="text" id="crawlUrlInput" placeholder="Bank Directory URL (e.g. https://www.dbs.com.sg/personal/cards/default.page)" value="https://www.dbs.com.sg/personal/cards/default.page">
        <div style="display:flex; gap:8px;">
          <input type="text" id="crawlIssuerInput" placeholder="Bank Name (e.g. DBS Bank)" value="DBS Bank" style="flex:1;">
          <input type="text" id="crawlPatternInput" placeholder="Regex filter (leave empty for smart auto-detect)" value="" style="flex:1;">
        </div>
        <label style="display:flex; align-items:center; gap:6px; font-size:0.85rem; cursor:pointer;">
          <input type="checkbox" id="crawlScrapeNowCheckbox" checked> Scrape discovered cards immediately
        </label>
        <button class="btn-success" onclick="triggerCrawlFromUi()">Discover & Scrape Bank</button>
      </div>
      <div id="crawlFeedback" style="margin-top:12px; font-size:0.85rem;"></div>
    </div>
  </div>

  <!-- Candidate Review Queue -->
  <div style="border-top:1px solid var(--border); padding-top:20px;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:12px;">
      <h3 style="font-size:1.2rem;">Review & Decisions Queue</h3>
      <div style="display:flex; gap:10px; align-items:center;">
        <select id="candidateStatusSelect" onchange="loadCandidates()">
          <option value="pending">Pending Review</option>
          <option value="approved">Approved</option>
          <option value="rejected">Rejected</option>
        </select>
        <button onclick="loadCandidates()">Refresh Queue</button>
      </div>
    </div>
    <div id="candidatesList"></div>
  </div>
</section>

<!-- Detail Modal -->
<div id="cardModal" class="modal">
  <div class="modal-content">
    <div class="modal-header">
      <h2 id="modalTitle" style="font-size:1.25rem;"></h2>
      <button class="close-btn" onclick="closeModal()">&times;</button>
    </div>
    <div id="modalBody"></div>
  </div>
</div>

<script>
let state = {
  cards: [],
  selectedForCompare: new Set(),
  debounceTimer: null,
  revision: 0
};

function showTab(tabId) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(el => el.classList.remove('active'));
  document.getElementById(tabId).classList.add('active');
  if (tabId === 'tabCards') document.getElementById('tabCardsBtn').classList.add('active');
  if (tabId === 'tabCompare') {
    document.getElementById('tabCompareBtn').classList.add('active');
    renderCompareSelector();
  }
  if (tabId === 'tabAdmin') document.getElementById('tabAdminBtn').classList.add('active');
}

function debounceLoadCards() {
  clearTimeout(state.debounceTimer);
  state.debounceTimer = setTimeout(loadCards, 250);
}

async function loadCards() {
  const q = document.getElementById('searchInput').value;
  const rewardType = document.getElementById('rewardTypeSelect').value;
  const support = document.getElementById('supportSelect').value;
  let url = '/api/v1/cards?limit=100';
  if (q) url += '&q=' + encodeURIComponent(q);
  if (rewardType) url += '&reward_type=' + encodeURIComponent(rewardType);
  if (support) url += '&simulation_support=' + encodeURIComponent(support);

  try {
    const res = await fetch(url);
    const data = await res.json();
    state.cards = data.items || [];
    state.revision = data.catalogue_revision || 0;
    document.getElementById('catalogueRevisionBadge').textContent = 'Catalogue Rev: ' + state.revision;
    renderCards();
  } catch (err) {
    document.getElementById('cardsList').innerHTML = '<p style="color:var(--danger)">Failed to load cards catalogue.</p>';
  }
}

function renderCards() {
  const container = document.getElementById('cardsList');
  if (!state.cards.length) {
    container.innerHTML = '<p style="color:var(--muted)">No published cards found matching filters.</p>';
    return;
  }
  container.innerHTML = state.cards.map(c => {
    const isChecked = state.selectedForCompare.has(c.card_id) ? 'checked' : '';
    return `
      <div class="card-item">
        <div>
          <div class="card-header">
            <div>
              <div class="card-title">${escapeHtml(c.name)}</div>
              <div class="card-issuer">${escapeHtml(c.bank?.name || 'Bank')} &bull; ${c.market || 'SG'}</div>
            </div>
            <span class="badge badge-${c.reward_type}">${c.reward_type}</span>
          </div>
          <div class="card-headline">${escapeHtml(c.headline || '')}</div>
          <div class="card-meta">
            <span>Annual Fee: ${c.currency || 'SGD'} ${c.annual_fee || '0.00'}</span>
            <span>Simulation: ${c.simulation_support}</span>
          </div>
        </div>
        <div class="card-actions">
          <button onclick="viewCardDetail('${c.card_id}')">View Details & Evidence</button>
          <label style="display:flex; align-items:center; gap:6px; font-size:0.8rem; cursor:pointer; margin-left:auto;">
            <input type="checkbox" ${isChecked} onchange="toggleCompare('${c.card_id}')"> Compare
          </label>
        </div>
      </div>
    `;
  }).join('');
}

function toggleCompare(cardId) {
  if (state.selectedForCompare.has(cardId)) {
    state.selectedForCompare.delete(cardId);
  } else {
    if (state.selectedForCompare.size >= 4) {
      alert('You can compare a maximum of 4 cards.');
      renderCards();
      return;
    }
    state.selectedForCompare.add(cardId);
  }
  document.getElementById('compareCount').textContent = state.selectedForCompare.size;
  renderCards();
}

function renderCompareSelector() {
  const container = document.getElementById('compareSelectGrid');
  container.innerHTML = state.cards.map(c => `
    <div class="card-item" style="padding:12px;">
      <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
        <input type="checkbox" ${state.selectedForCompare.has(c.card_id) ? 'checked' : ''} onchange="toggleCompare('${c.card_id}'); renderCompareSelector();">
        <strong>${escapeHtml(c.name)}</strong> (${c.reward_type})
      </label>
    </div>
  `).join('');
}

async function runComparison() {
  const ids = Array.from(state.selectedForCompare);
  if (ids.length < 2 || ids.length > 4) {
    alert('Please select between 2 and 4 cards to compare.');
    return;
  }
  const resultsDiv = document.getElementById('compareResults');
  resultsDiv.innerHTML = '<p>Computing side-by-side comparison...</p>';

  try {
    const res = await fetch('/api/v1/cards/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ card_ids: ids })
    });
    if (!res.ok) {
      const err = await res.json();
      resultsDiv.innerHTML = `<p style="color:var(--danger)">Error: ${err.error?.message || 'Comparison failed'}</p>`;
      return;
    }
    const data = await res.json();
    let html = `<h3>Comparing ${data.cards.length} Cards (Catalogue Revision ${data.catalogue_revision})</h3>`;
    html += '<table><thead><tr><th>Card Name</th>' + data.cards.map(c => `<th>${escapeHtml(c.card.name)}</th>`).join('') + '</tr></thead><tbody>';
    html += '<tr><td><strong>Annual Fee</strong></td>' + data.cards.map(c => `<td>${c.card.currency} ${c.terms.annual_fee}</td>`).join('') + '</tr>';
    html += '<tr><td><strong>First Year Waiver</strong></td>' + data.cards.map(c => `<td>${c.terms.first_year_waiver}</td>`).join('') + '</tr>';
    html += '<tr><td><strong>Base Reward Rate</strong></td>' + data.cards.map(c => `<td>${(c.terms.rules[0]?.rate * 100).toFixed(2)}% (${c.terms.rules[0]?.kind})</td>`).join('') + '</tr>';
    html += '</tbody></table>';

    if (data.differences?.length) {
      html += '<h4 style="margin-top:20px;">Detected Field Differences:</h4>';
      html += '<table><thead><tr><th>Field</th><th>Comparison Values</th></tr></thead><tbody>';
      data.differences.forEach(d => {
        html += `<tr><td><strong>${d.field}</strong></td><td><pre>${escapeHtml(JSON.stringify(d.values, null, 2))}</pre></td></tr>`;
      });
      html += '</tbody></table>';
    }
    resultsDiv.innerHTML = html;
  } catch (err) {
    resultsDiv.innerHTML = '<p style="color:var(--danger)">Failed to perform comparison.</p>';
  }
}

async function viewCardDetail(cardId) {
  try {
    const res = await fetch('/api/v1/cards/' + cardId);
    if (!res.ok) return alert('Card not found');
    const data = await res.json();
    document.getElementById('modalTitle').textContent = data.card.name;
    let body = `
      <p style="color:var(--accent); font-weight:600; margin-bottom:12px;">${escapeHtml(data.card.headline)}</p>
      <div style="font-size:0.85rem; color:var(--muted); margin-bottom:16px;">
        Issuer: ${escapeHtml(data.card.bank.name)} | Market: ${data.card.market} | Revision: ${data.catalogue_revision}
      </div>
      <h4>Reward Rules:</h4>
      <table>
        <thead><tr><th>Rule Key</th><th>Kind</th><th>Rate</th><th>Unit</th><th>Policy</th></tr></thead>
        <tbody>
          ${data.terms.rules.map(r => `<tr><td>${r.rule_key}</td><td>${r.kind}</td><td><strong>${r.rate}</strong></td><td>${r.reward_unit}</td><td>${r.stacking_policy}</td></tr>`).join('')}
        </tbody>
      </table>
      <h4 style="margin-top:16px;">Verifiable Evidence Citations:</h4>
      <table>
        <thead><tr><th>Field</th><th>Snippet</th><th>Source URL</th></tr></thead>
        <tbody>
          ${(data.evidence || []).map(e => `<tr><td>${e.field_path}</td><td><em>"${escapeHtml(e.snippet)}"</em></td><td><a href="${e.source_url}" target="_blank" style="color:var(--primary);">${escapeHtml(e.document_type)}</a></td></tr>`).join('')}
        </tbody>
      </table>
      <h4 style="margin-top:16px;">Full Terms Payload:</h4>
      <pre>${escapeHtml(JSON.stringify(data.terms, null, 2))}</pre>
    `;
    document.getElementById('modalBody').innerHTML = body;
    document.getElementById('cardModal').classList.add('open');
  } catch (err) {
    alert('Error loading card details');
  }
}

async function loadCandidates() {
  const secret = document.getElementById('adminSecretInput').value.trim();
  const status = document.getElementById('candidateStatusSelect').value;
  const container = document.getElementById('candidatesList');
  container.innerHTML = '<p>Loading extraction candidates...</p>';

  try {
    const res = await fetch('/api/v1/admin/extraction-candidates?review_status=' + encodeURIComponent(status), {
      headers: { 'Authorization': 'Bearer ' + secret }
    });
    if (res.status === 401 || res.status === 503) {
      container.innerHTML = '<p style="color:var(--danger)">Authentication failed. Please provide a valid CARD_ADMIN_BEARER token.</p>';
      return;
    }
    const data = await res.json();
    const items = data.items || [];
    if (!items.length) {
      container.innerHTML = `<p style="color:var(--muted)">No candidates found with status: ${status}</p>`;
      return;
    }

    container.innerHTML = items.map(cand => `
      <div class="card-item" style="margin-bottom:16px;">
        <div class="card-header">
          <div>
            <div class="card-title">${escapeHtml(cand.card?.name || 'Candidate')}</div>
            <div class="card-issuer">ID: ${cand.candidate_id} &bull; Revision: ${cand.review_revision}</div>
          </div>
          <span class="badge badge-${cand.review_status}">${cand.review_status}</span>
        </div>
        <p><strong>Headline:</strong> ${escapeHtml(cand.card?.headline || '')}</p>
        <p><strong>Content Hash:</strong> <code>${cand.content_hash}</code></p>
        ${cand.diff?.length ? `
          <h5 style="margin-top:8px;">Diff vs Base Version:</h5>
          <table><thead><tr><th>Op</th><th>Field</th><th>Old</th><th>New</th></tr></thead><tbody>
          ${cand.diff.map(d => `<tr><td class="diff-${d.operation}">${d.operation}</td><td>${d.field_path}</td><td>${d.old_value}</td><td><strong>${d.new_value}</strong></td></tr>`).join('')}
          </tbody></table>
        ` : '<p style="color:var(--muted); margin-top:8px;">No material changes detected.</p>'}
        ${cand.review_status === 'pending' ? `
          <div class="card-actions" style="margin-top:12px;">
            <button class="btn-success" onclick="decideCandidate('${cand.candidate_id}', ${cand.review_revision}, 'approve')">Approve & Publish</button>
            <button class="btn-danger" onclick="decideCandidate('${cand.candidate_id}', ${cand.review_revision}, 'reject')">Reject</button>
          </div>
        ` : ''}
      </div>
    `).join('');
  } catch (err) {
    container.innerHTML = '<p style="color:var(--danger)">Failed to fetch extraction candidates.</p>';
  }
}

async function decideCandidate(candidateId, revision, decision) {
  const secret = document.getElementById('adminSecretInput').value.trim();
  const reason = prompt(`Enter reason for ${decision}:`, 'Verified via admin review UI.');
  if (!reason) return;

  try {
    const res = await fetch(`/api/v1/admin/extraction-candidates/${candidateId}/decisions`, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + secret,
        'Content-Type': 'application/json',
        'Idempotency-Key': 'ui-decision-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8)
      },
      body: JSON.stringify({
        expected_review_revision: revision,
        decision: decision,
        reason: reason,
        acknowledged_warning_codes: []
      })
    });
    if (res.ok) {
      alert(`Candidate successfully ${decision}d!`);
      loadCandidates();
      loadCards();
    } else {
      const err = await res.json();
      alert('Decision failed: ' + (err.error?.message || res.statusText));
    }
  } catch (err) {
    alert('Failed to submit decision: ' + err.message);
  }
}

async function onAdminSecretChange() {
  await loadConfigSources();
  await loadCandidates();
}

async function loadConfigSources() {
  const secret = document.getElementById('adminSecretInput').value.trim();
  if (!secret) return;
  try {
    const res = await fetch('/api/v1/admin/config-sources', {
      headers: { 'Authorization': 'Bearer ' + secret }
    });
    if (res.ok) {
      const data = await res.json();
      const select = document.getElementById('configSourceSelect');
      select.innerHTML = '<option value="">All Configured Sources (' + data.sources.length + ')</option>' +
        data.sources.map(s => `<option value="${s.source_id}">${escapeHtml(s.name)} (${s.issuer})</option>`).join('');
    }
  } catch (e) {}
}

async function triggerScrapeFromUi() {
  const secret = document.getElementById('adminSecretInput').value.trim();
  if (!secret) {
    alert('Please enter your CARD_ADMIN_BEARER token first.');
    return;
  }
  const sourceId = document.getElementById('configSourceSelect').value;
  const feedback = document.getElementById('scrapeFeedback');
  feedback.innerHTML = '<span style="color:var(--primary)">Scraping in progress...</span>';

  try {
    const body = sourceId ? { source_ids: [sourceId] } : {};
    const res = await fetch('/v1/scrape', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + secret,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (res.ok) {
      feedback.innerHTML = `<span style="color:var(--accent)">Completed! Processed ${data.results?.length || 0} source(s).</span>`;
      loadCards();
      loadCandidates();
    } else {
      feedback.innerHTML = `<span style="color:var(--danger)">Scrape failed: ${data.error?.message || 'Error'}</span>`;
    }
  } catch (err) {
    feedback.innerHTML = `<span style="color:var(--danger)">Error: ${err.message}</span>`;
  }
}

function setBankPreset(bank) {
  if (bank === 'dbs') {
    document.getElementById('crawlUrlInput').value = 'https://www.dbs.com.sg/personal/cards/default.page';
    document.getElementById('crawlIssuerInput').value = 'DBS Bank';
    document.getElementById('crawlPatternInput').value = '';
  } else if (bank === 'ocbc') {
    document.getElementById('crawlUrlInput').value = 'https://www.ocbc.com/personal-banking/cards';
    document.getElementById('crawlIssuerInput').value = 'OCBC Bank';
    document.getElementById('crawlPatternInput').value = '';
  } else if (bank === 'uob') {
    document.getElementById('crawlUrlInput').value = 'https://www.uob.com.sg/personal/cards/index.page';
    document.getElementById('crawlIssuerInput').value = 'UOB Bank';
    document.getElementById('crawlPatternInput').value = '';
  }
}

async function triggerCrawlFromUi() {
  const secret = document.getElementById('adminSecretInput').value.trim();
  if (!secret) {
    alert('Please enter your CARD_ADMIN_BEARER token first.');
    return;
  }
  const url = document.getElementById('crawlUrlInput').value.trim();
  const issuer = document.getElementById('crawlIssuerInput').value.trim();
  const pattern = document.getElementById('crawlPatternInput').value.trim();
  const scrapeNow = document.getElementById('crawlScrapeNowCheckbox').checked;
  const feedback = document.getElementById('crawlFeedback');

  if (!url) {
    alert('Please provide a bank directory URL.');
    return;
  }
  feedback.innerHTML = '<span style="color:var(--accent)">Crawling bank directory & discovering cards...</span>';

  try {
    const res = await fetch('/api/v1/admin/discover', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + secret,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        url: url,
        issuer: issuer || 'Bank',
        pattern: pattern || null,
        scrape_now: scrapeNow
      })
    });
    const data = await res.json();
    if (res.ok) {
      let cardList = (data.cards || []).map(c => `<li><a href="${c.url}" target="_blank" style="color:var(--primary); font-weight:500;">${escapeHtml(c.title || c.slug)}</a> <span style="color:var(--muted); font-size:0.75rem;">(${c.slug})</span></li>`).join('');
      feedback.innerHTML = `<span style="color:var(--accent); font-weight:600;">Success! Discovered ${data.discovered_count} cards${scrapeNow ? `, scraped ${data.scraped_count}` : ''}.</span>` +
        (cardList ? `<ul style="margin-top:8px; padding-left:18px; font-size:0.85rem; max-height:120px; overflow-y:auto; line-height:1.6;">${cardList}</ul>` : '');
      loadCards();
      loadCandidates();
    } else {
      feedback.innerHTML = `<span style="color:var(--danger)">Crawl failed: ${data.error?.message || 'Error'}</span>`;
    }
  } catch (err) {
    feedback.innerHTML = `<span style="color:var(--danger)">Error: ${err.message}</span>`;
  }
}

function closeModal() {
  document.getElementById('cardModal').classList.remove('open');
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Initial load
loadCards();
</script>

</body>
</html>
"""


def make_handler(
    config: dict,
    db_path: str,
    api_key: str | None,
    allow_private_hosts: bool,
    *,
    admin_secret: str | None = None,
    internal_secret: str | None = None,
):
    """Factory for HTTP request handler with authentication, validation, and caching."""
    if api_key == "":
        raise ValueError("api_key must be nonempty when supplied")

    admin_secret = admin_secret or api_key or os.getenv("CARD_ADMIN_BEARER")
    internal_secret = internal_secret or os.getenv("CARD_INTERNAL_BEARER")
    cursor_secret = os.getenv("CARD_CURSOR_HMAC_SECRET") or admin_secret or "local-development-cursor-key"

    class Handler(BaseHTTPRequestHandler):
        server_version = "CardCatalogue/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            """Suppress default stderr request logging to avoid leaking sensitive headers."""
            return

        def _rid(self) -> str:
            """Extract valid UUID X-Request-ID or generate a new UUID4."""
            incoming = self.headers.get("X-Request-ID", "")
            return incoming if _uuid(incoming) else str(uuid.uuid4())

        def _send(
            self,
            status: int,
            payload: object | None,
            headers: dict[str, str] | None = None,
        ) -> None:
            """Send HTTP response with headers and serialized payload."""
            raw = b"" if payload is None else _json(payload)
            self.send_response(status)
            self.send_header("X-Request-ID", self.request_id)
            self.send_header("Content-Length", str(len(raw)))
            if payload is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def _send_html(self, status: int, html_content: str) -> None:
            """Send UTF-8 HTML response."""
            raw = html_content.encode("utf-8")
            self.send_response(status)
            self.send_header("X-Request-ID", self.request_id)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _error(
            self,
            status: int,
            code: str,
            message: str,
            details: list | None = None,
        ) -> None:
            """Emit standard structured RFC-style JSON error response."""
            self._send(
                status,
                {
                    "error": {
                        "code": code,
                        "message": message,
                        "details": details or [],
                        "request_id": self.request_id,
                    }
                },
            )

        def _auth(self, kind: str) -> str | None:
            """Authenticate caller against configured admin or internal secret."""
            secret = admin_secret if kind == "admin" else internal_secret
            auth_header = self.headers.get("Authorization", "")
            token = auth_header[7:] if auth_header.startswith("Bearer ") else ""

            if not secret:
                self._error(503, "SERVICE_UNAVAILABLE", "Required server identity is not configured.")
                return None
            if not hmac.compare_digest(token, secret):
                self._error(401, "UNAUTHENTICATED", "Missing or invalid bearer token.")
                return None
            return kind

        def _body(self, required: set[str], optional: set[str] = set()) -> dict | None:
            """Parse and validate JSON request body with key whitelist."""
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type != "application/json":
                self._error(415, "UNSUPPORTED_MEDIA_TYPE", "JSON Content-Type is required.")
                return None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_BODY:
                    raise ValueError
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict):
                    raise TypeError
                keys = set(data.keys())
                if keys - required - optional or not required <= keys:
                    raise TypeError
                return data
            except (ValueError, TypeError, json.JSONDecodeError):
                self._error(400, "MALFORMED_REQUEST", "Malformed JSON request.")
                return None

        def _page(
            self,
            items: list,
            filters: dict,
            revision: int,
            default: int = 20,
        ) -> dict | None:
            """Slice items list using HMAC cursor pagination."""
            query_params = parse_qs(urlparse(self.path).query)
            limit_raw = (query_params.get("limit") or [str(default)])[0]

            try:
                limit = int(limit_raw)
                assert 1 <= limit <= 100
            except (ValueError, AssertionError):
                self._error(422, "VALIDATION_ERROR", "limit must be an integer from 1 to 100.")
                return None

            cursor_token = (query_params.get("cursor") or [""])[0]
            try:
                offset = _decode_cursor(cursor_secret, cursor_token, filters, revision) if cursor_token else 0
            except LookupError:
                self._error(422, "CURSOR_FILTER_MISMATCH", "Cursor is bound to different filters.")
                return None
            except RuntimeError:
                self._error(409, "CURSOR_EXPIRED", "Cursor expired or catalogue changed.")
                return None
            except ValueError:
                self._error(422, "VALIDATION_ERROR", "Invalid cursor.")
                return None

            selected = items[offset : offset + limit]
            next_cursor = (
                _cursor(cursor_secret, filters, revision, offset + limit)
                if offset + limit < len(items)
                else None
            )
            return {"items": selected, "next_cursor": next_cursor}

        def _conditional(self, revision: int, representation: str) -> bool:
            """Check If-None-Match header; respond with 304 Not Modified if matched."""
            tag = _etag(revision, representation)
            if self.headers.get("If-None-Match") == tag:
                self._send(304, None, {"ETag": tag})
                return True
            self.response_etag = tag
            return False

        def do_GET(self) -> None:
            """Handle incoming GET requests."""
            self.request_id = self._rid()
            self.response_etag = None
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            try:
                # Dashboard UI
                if path in ("/", "/ui", "/dashboard"):
                    self._send_html(200, DASHBOARD_HTML)
                    return

                # Health and Metrics
                if path == "/health":
                    db.initialize(db_path)
                    self._send(200, {"status": "ok", "database": "ready"})
                    return

                if path == "/metrics":
                    self._send(200, {"service": "card_catalogue", "mode": "sqlite", "note": "Prometheus text is available at /metrics/prometheus"})
                    return

                if path == "/metrics/prometheus":
                    self._send(200, None, {"Content-Type": "text/plain; version=0.0.4", "Cache-Control": "no-store"})
                    return

                # Legacy compatibility endpoints
                if path == "/v1/cards":
                    issuer = (query.get("issuer") or [None])[0]
                    source_id = (query.get("source_id") or [None])[0]
                    self._send(200, {"cards": db.cards(db_path, issuer, source_id)})
                    return

                if path.startswith("/v1/cards/"):
                    card_id = unquote(path[10:])
                    item = db.card(db_path, card_id)
                    if item:
                        self._send(200, item)
                    else:
                        self._error(404, "NOT_FOUND", "Card not found.")
                    return

                if path == "/v1/scrape-runs":
                    limit = int((query.get("limit") or ["50"])[0])
                    self._send(200, {"scrape_runs": db.runs(db_path, limit)})
                    return

                # Modern v1 Public Catalogue Endpoints
                if path == "/api/v1/cards":
                    filters = {
                        k: (query.get(k) or [None])[0]
                        for k in ("q", "bank_id", "reward_type", "simulation_support", "freshness")
                    }
                    filters = {k: v for k, v in filters.items() if v is not None}

                    if filters.get("bank_id") and not _uuid(filters["bank_id"]):
                        self._error(422, "VALIDATION_ERROR", "bank_id must be UUID.")
                        return

                    if filters.get("q") and len(filters["q"]) > 100:
                        self._error(422, "VALIDATION_ERROR", "q is too long.")
                        return

                    valid_enums = {
                        "reward_type": {"cashback", "miles", "points", "mixed"},
                        "simulation_support": {"supported", "unsupported"},
                        "freshness": {"fresh", "stale"},
                    }
                    if any(filters.get(k) not in valid_enums[k] for k in valid_enums if filters.get(k)):
                        self._error(422, "VALIDATION_ERROR", "Invalid filter enum.")
                        return

                    rev, items = db.published_cards(db_path, filters)
                    if self._conditional(rev, "cards:" + json.dumps(filters, sort_keys=True)):
                        return

                    page = self._page(items, filters, rev)
                    if page:
                        self._send(200, {"catalogue_revision": rev, **page}, {"ETag": self.response_etag})
                    return

                if path.startswith("/api/v1/cards/"):
                    cid = unquote(path[len("/api/v1/cards/") :])
                    if not _uuid(cid):
                        self._error(422, "VALIDATION_ERROR", "card_id must be UUID.")
                        return

                    detail = db.current_detail(db_path, cid)
                    if not detail:
                        self._error(404, "NOT_FOUND", "Card is unavailable.")
                        return

                    if self._conditional(detail["catalogue_revision"], "detail:" + cid):
                        return

                    self._send(
                        200,
                        {
                            "catalogue_revision": detail["catalogue_revision"],
                            "card": detail["card"],
                            "terms": detail["terms"],
                            "effective_from": detail["effective_from"],
                            "effective_to": detail["effective_to"],
                            "evidence": detail["evidence"],
                        },
                        {"ETag": self.response_etag},
                    )
                    return

                # Recommendation Engine Internal Endpoints
                if path == "/api/v1/internal/catalogue-snapshots/current":
                    if not self._auth("internal"):
                        return
                    schema = (query.get("schema_version") or ["card_terms.v1"])[0]
                    support = (query.get("simulation_support") or ["supported"])[0]
                    market = (query.get("market") or ["SG"])[0]
                    reward = (query.get("reward_type") or [None])[0]

                    if schema != "card_terms.v1" or support not in {"supported", "all"}:
                        self._error(422, "UNSUPPORTED_SCHEMA_VERSION", "Unsupported schema or support filter.")
                        return

                    filter_arg = {"reward_type": reward} if reward else {}
                    rev, items = db.published_cards(db_path, filter_arg)
                    details = [
                        db.current_detail(db_path, x["card_id"])
                        for x in items
                        if support == "all" or x["simulation_support"] == "supported"
                    ]
                    cards_list = [
                        {
                            "card": x["card"],
                            "terms": x["terms"],
                            "effective_from": x["effective_from"],
                            "effective_to": x["effective_to"],
                            "evidence": x["evidence"],
                        }
                        for x in sorted(
                            (x for x in details if x and x["card"]["market"] == market),
                            key=lambda item: item["card"]["card_id"],
                        )
                    ]
                    payload = {
                        "schema_version": "card_catalogue_snapshot.v1",
                        "card_terms_schema_version": "card_terms.v1",
                        "catalogue_revision": rev,
                        "generated_at": utc_now(),
                        "cards": cards_list,
                    }

                    if self._conditional(rev, f"snapshot:{schema}{support}{market}{reward}"):
                        return

                    self._send(200, payload, {"ETag": self.response_etag, "Cache-Control": "no-store"})
                    return

                if path.startswith("/api/v1/internal/card-versions/"):
                    if not self._auth("internal"):
                        return
                    vid = unquote(path.rsplit("/", 1)[-1])
                    if not _uuid(vid):
                        self._error(422, "VALIDATION_ERROR", "card_version_id must be UUID.")
                        return

                    with db.connect(db_path) as conn:
                        row = conn.execute(
                            """
                            SELECT p.catalogue_revision, p.superseded_revision, v.*
                            FROM card_versions v
                            JOIN catalogue_publications p ON p.card_version_id = v.card_version_id
                            WHERE v.card_version_id = ?
                            """,
                            (vid,),
                        ).fetchone()
                        evidence = [
                            dict(e)
                            for e in conn.execute(
                                """
                                SELECT evidence_id, field_path, source_url, document_type, locator, snippet, fetched_at
                                FROM evidence_links WHERE card_version_id = ?
                                """,
                                (vid,),
                            )
                        ]

                    if not row:
                        self._error(404, "NOT_FOUND", "Version unavailable.")
                        return

                    self._send(
                        200,
                        {
                            "published_in_catalogue_revision": row["catalogue_revision"],
                            "superseded_in_catalogue_revision": row["superseded_revision"],
                            "card": json.loads(row["summary_json"]),
                            "terms": json.loads(row["terms_json"]),
                            "effective_from": row["effective_from"],
                            "effective_to": row["effective_to"],
                            "evidence": evidence,
                        },
                        {"Cache-Control": "no-store"},
                    )
                    return

                # Admin Sources and Runs
                if path == "/api/v1/admin/config-sources":
                    if not self._auth("admin"):
                        return
                    self._send(200, {"sources": config.get("sources", [])}, {"Cache-Control": "no-store"})
                    return

                if path == "/api/v1/admin/sources":
                    if not self._auth("admin"):
                        return
                    rows = db.source_list(db_path)
                    filters = {
                        k: (query.get(k) or [None])[0]
                        for k in ("kind", "access_review_status", "freshness")
                    }
                    filters = {k: v for k, v in filters.items() if v is not None}
                    filtered = [row for row in rows if all(str(row.get(k)) == str(v) for k, v in filters.items())]
                    page = self._page(filtered, filters, 0)
                    self._send(200, page, {"Cache-Control": "no-store"})
                    return

                if path.startswith("/api/v1/admin/scrape-runs/"):
                    if not self._auth("admin"):
                        return
                    rid = unquote(path.rsplit("/", 1)[-1])
                    if not _uuid(rid):
                        self._error(422, "VALIDATION_ERROR", "run_id must be UUID.")
                        return
                    item = db.run(db_path, rid)
                    if item:
                        self._send(200, item, {"Cache-Control": "no-store"})
                    else:
                        self._error(404, "NOT_FOUND", "Run unavailable.")
                    return

                # Admin Extraction Candidates
                if path == "/api/v1/admin/extraction-candidates":
                    if not self._auth("admin"):
                        return
                    filters = {
                        k: (query.get(k) or [None])[0]
                        for k in ("review_status", "source_id", "scrape_run_id", "bank_id", "material_change")
                    }
                    parsed_filters = {
                        k: (v == "true" if k == "material_change" else v)
                        for k, v in filters.items()
                        if v is not None
                    }
                    items = db.candidate_list(db_path, parsed_filters)
                    page = self._page(items, parsed_filters, 0, 10)
                    self._send(200, page, {"Cache-Control": "no-store"})
                    return

                if path.startswith("/api/v1/admin/extraction-candidates/"):
                    if not self._auth("admin"):
                        return
                    candidate_id = unquote(path.rsplit("/", 1)[-1])
                    if not _uuid(candidate_id):
                        self._error(422, "VALIDATION_ERROR", "candidate_id must be UUID.")
                        return
                    candidate = db.get_candidate(db_path, candidate_id)
                    if candidate:
                        self._send(200, candidate, {"Cache-Control": "no-store"})
                    else:
                        self._error(404, "NOT_FOUND", "Candidate unavailable.")
                    return

                self._error(404, "NOT_FOUND", "Route not found.")

            except (ValueError, ConfigError):
                self._error(400, "MALFORMED_REQUEST", "Invalid request.")
            except Exception:
                if path in {"/v1/cards", "/v1/scrape-runs"} or path.startswith("/v1/cards/"):
                    self._error(500, "INTERNAL_ERROR", "database query failed")
                else:
                    self._error(500, "INTERNAL_ERROR", "Request could not be completed.")

        def do_POST(self) -> None:
            """Handle incoming POST requests."""
            self.request_id = self._rid()
            path = urlparse(self.path).path

            # Scrape endpoint
            if path in ("/v1/scrape", "/api/v1/admin/scrape"):
                if (admin_secret or api_key) and not self._auth("admin"):
                    return
                body = self._body(set(), {"source_ids"})
                if body is None:
                    return
                try:
                    ids = body.get("source_ids")
                    if ids is not None and (not isinstance(ids, list) or not all(isinstance(x, str) for x in ids)):
                        raise ValueError
                    results = scrape_sources(selected_sources(config, ids if ids else None), db_path, allow_private_hosts)
                    self._send(200, {"status": "completed", "results": results})
                except Exception as exc:
                    self._error(400, "MALFORMED_REQUEST", f"Invalid scrape request: {exc}")
                return

            # Live Bank Directory Crawler & Discovery
            if path == "/api/v1/admin/discover":
                if not self._auth("admin"):
                    return
                body = self._body({"url"}, {"issuer", "pattern", "scrape_now"})
                if body is None:
                    return
                from .scraper import Scraper
                scraper = Scraper(allow_private_hosts=allow_private_hosts)
                try:
                    pattern = body.get("pattern")
                    if pattern is not None and not str(pattern).strip():
                        pattern = None
                    discovered = scraper.discover_links(body["url"], pattern)
                    scraped_results = []
                    if body.get("scrape_now"):
                        sources_to_scrape = []
                        issuer = body.get("issuer") or "Discovered Bank"
                        for item in discovered:
                            slug = item["slug"]
                            source_id = f"{issuer.lower().replace(' ', '-')}-{slug}"
                            sources_to_scrape.append({
                                "source_id": source_id,
                                "issuer": issuer,
                                "name": item["title"],
                                "card_id": source_id,
                                "page_url": item["url"],
                                "enabled": True,
                                "request_delay_seconds": 1,
                                "extraction_hints": {},
                            })
                        scraped_results = scrape_sources(sources_to_scrape, db_path, allow_private_hosts)
                    self._send(
                        200,
                        {
                            "status": "success",
                            "discovered_count": len(discovered),
                            "cards": discovered,
                            "scraped_count": len(scraped_results),
                            "results": scraped_results,
                        },
                        {"Cache-Control": "no-store"},
                    )
                except Exception as exc:
                    self._error(400, "DISCOVERY_FAILED", str(exc))
                return

            # Public Card Comparison
            if path == "/api/v1/cards/compare":
                body = self._body({"card_ids"})
                if body is None:
                    return
                ids = body["card_ids"]
                if (
                    not isinstance(ids, list)
                    or not 2 <= len(ids) <= 4
                    or len(set(ids)) != len(ids)
                    or not all(_uuid(x) for x in ids)
                ):
                    self._error(422, "VALIDATION_ERROR", "card_ids must contain 2-4 distinct UUIDs.")
                    return

                cards_detail = [db.current_detail(db_path, x) for x in ids]
                if any(x is None for x in cards_detail):
                    self._error(404, "NOT_FOUND", "A requested card is unavailable.")
                    return

                rev = cards_detail[0]["catalogue_revision"]
                diff_fields = {
                    "annual_fee": lambda x: x.get("terms", {}).get("annual_fee"),
                    "first_year_waiver": lambda x: x.get("terms", {}).get("first_year_waiver"),
                    "eligibility": lambda x: x.get("terms", {}).get("eligibility"),
                    "rewards": lambda x: x.get("terms", {}).get("rules", []),
                    "caps": lambda x: x.get("terms", {}).get("cap_groups", []),
                    "benefits": lambda x: x.get("terms", {}).get("benefits", []),
                }
                differences = []
                for field_key, extractor in diff_fields.items():
                    distinct_values = {json.dumps(extractor(card), sort_keys=True) for card in cards_detail}
                    if len(distinct_values) > 1:
                        differences.append(
                            {
                                "field": field_key,
                                "values": [
                                    {
                                        "card_id": card["card"]["card_id"],
                                        "display_value": json.dumps(extractor(card), sort_keys=True),
                                    }
                                    for card in cards_detail
                                ],
                            }
                        )

                self._send(
                    200,
                    {
                        "catalogue_revision": rev,
                        "cards": [
                            {
                                "catalogue_revision": card["catalogue_revision"],
                                "card": card["card"],
                                "terms": card["terms"],
                                "effective_from": card["effective_from"],
                                "effective_to": card["effective_to"],
                                "evidence": card["evidence"],
                            }
                            for card in cards_detail
                        ],
                        "differences": differences,
                    },
                )
                return

            # Admin Queue Scrape Run
            if path == "/api/v1/admin/scrape-runs":
                if not self._auth("admin"):
                    return
                body = self._body({"source_id", "scope", "reason"})
                key = self.headers.get("Idempotency-Key", "")
                if body is None:
                    return

                if not key or len(key) > 255:
                    self._error(422, "VALIDATION_ERROR", "Idempotency-Key is required.")
                    return

                try:
                    if (
                        not _uuid(body["source_id"])
                        or body["reason"] not in {"scheduled_refresh", "admin_refresh", "parser_recheck"}
                        or not isinstance(body["scope"], dict)
                        or body["scope"].get("type") not in {"all", "documents"}
                    ):
                        raise ValueError

                    body_hash = hashlib.sha256(_json(body)).hexdigest()
                    cached = db.idempotency_get(db_path, "admin", path, key, body_hash)
                    if cached:
                        self._send(cached[0], cached[2], cached[1])
                        return

                    result = db.queue_run(db_path, body["source_id"], body["scope"], body["reason"])
                    headers = {"Cache-Control": "no-store"}
                    db.idempotency_put(db_path, "admin", path, key, body_hash, 202, headers, result)
                    self._send(202, result, headers)
                except RuntimeError as exc:
                    err_msg = str(exc)
                    self._error(
                        409,
                        "IDEMPOTENCY_CONFLICT" if err_msg == "IDEMPOTENCY_CONFLICT" else "SOURCE_ALREADY_RUNNING",
                        "Request conflicts with existing state.",
                        [{"run_id": err_msg.split(":")[-1]}],
                    )
                except ValueError:
                    self._error(422, "VALIDATION_ERROR", "Invalid registered source or scope.")
                return

            # Admin Review Decisions
            if path.endswith("/decisions") and path.startswith("/api/v1/admin/extraction-candidates/"):
                if not self._auth("admin"):
                    return
                candidate_id = path.split("/")[-2]
                body = self._body({"expected_review_revision", "decision", "reason", "acknowledged_warning_codes"})
                key = self.headers.get("Idempotency-Key", "")
                if body is None:
                    return

                if (
                    not key
                    or not _uuid(candidate_id)
                    or body.get("decision") not in {"approve", "reject"}
                    or not isinstance(body.get("expected_review_revision"), int)
                    or not isinstance(body.get("reason"), str)
                    or not 1 <= len(body["reason"]) <= 1000
                    or not isinstance(body.get("acknowledged_warning_codes"), list)
                ):
                    self._error(422, "VALIDATION_ERROR", "Invalid decision request.")
                    return

                try:
                    body_hash = hashlib.sha256(_json(body)).hexdigest()
                    cached = db.idempotency_get(db_path, "admin", path, key, body_hash)
                    if cached:
                        self._send(cached[0], cached[2], cached[1])
                        return

                    status, result = db.decide(
                        db_path,
                        candidate_id,
                        body["expected_review_revision"],
                        body["decision"],
                        body["reason"],
                        body["acknowledged_warning_codes"],
                        "admin",
                    )
                    headers = {"Cache-Control": "no-store"}
                    db.idempotency_put(db_path, "admin", path, key, body_hash, status, headers, result)
                    self._send(status, result, headers)
                except LookupError:
                    self._error(404, "NOT_FOUND", "Candidate unavailable.")
                except RuntimeError as exc:
                    self._error(409, str(exc).split(":")[0], "Review state changed.")
                except ValueError:
                    self._error(422, "PUBLICATION_VALIDATION_FAILED", "Candidate cannot be published.")
                return

            self._error(404, "NOT_FOUND", "Route not found.")

        def do_PATCH(self) -> None:
            """Handle incoming PATCH requests for candidate modifications."""
            self.request_id = self._rid()
            path = urlparse(self.path).path

            if not (path.startswith("/api/v1/admin/extraction-candidates/") and path.count("/") == 5):
                self._error(404, "NOT_FOUND", "Route not found.")
                return

            if not self._auth("admin"):
                return

            candidate_id = path.rsplit("/", 1)[-1]
            body = self._body({"expected_review_revision", "edits", "reason"})
            if body is None:
                return

            if (
                not _uuid(candidate_id)
                or not isinstance(body["expected_review_revision"], int)
                or not isinstance(body["edits"], list)
                or not 1 <= len(body["edits"]) <= 50
                or not isinstance(body["reason"], str)
            ):
                self._error(422, "VALIDATION_ERROR", "Invalid candidate patch.")
                return

            if any(not isinstance(x, dict) or set(x.keys()) != {"field_path", "value"} for x in body["edits"]):
                self._error(422, "VALIDATION_ERROR", "Invalid candidate edits.")
                return

            try:
                updated = db.patch_candidate(
                    db_path,
                    candidate_id,
                    body["expected_review_revision"],
                    body["edits"],
                    body["reason"],
                    "admin",
                )
                self._send(200, updated, {"Cache-Control": "no-store"})
            except LookupError:
                self._error(404, "NOT_FOUND", "Candidate unavailable.")
            except RuntimeError as exc:
                self._error(409, str(exc).split(":")[0], "Review state changed.")
            except ValueError:
                self._error(422, "VALIDATION_ERROR", "Invalid candidate edits.")

        def do_DELETE(self) -> None:
            """Handle incoming DELETE requests."""
            self.request_id = self._rid()
            self._error(404, "NOT_FOUND", "Route not found.")

    return Handler


def serve(
    config: dict,
    db_path: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    api_key: str | None = None,
    allow_private_hosts: bool = False,
    *,
    admin_secret: str | None = None,
    internal_secret: str | None = None,
) -> None:
    """Start threaded HTTP server serving card catalogue and ingestion routes."""
    db.initialize(db_path)
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(
            config,
            db_path,
            api_key,
            allow_private_hosts,
            admin_secret=admin_secret,
            internal_secret=internal_secret,
        ),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
