"""Type-safe FastAPI HTTP API with boundary validation, Pydantic models, and OpenAPI docs.

Implements public catalogue discovery endpoints, internal recommendation snapshots,
admin operations for scraping and review workflows, and a built-in testing UI.
"""

from __future__ import annotations

import asyncio
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

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import db
from .config import ConfigError, selected_sources
from .scraper import Scraper, scrape_sources
from .util import utc_now

MAX_BODY = 64_000


class SpendItem(BaseModel):
    amount: str = Field(description="Transaction amount in major currency units, e.g. 50.00")
    category: str | None = Field(default=None, description="Category name (dining, groceries, retail, etc.)")
    mcc: str | None = Field(default=None, description="Merchant Category Code (4 digits)")
    description: str | None = Field(default=None, description="Transaction description")
    foreign_currency: bool | None = Field(default=False, description="Whether transaction was in foreign currency")


class SimulateRequest(BaseModel):
    monthly_spend: list[SpendItem] = Field(description="List of monthly transaction spend items")
    card_ids: list[str] | None = Field(default=None, description="Optional card IDs to simulate against")


class CompareRequest(BaseModel):
    card_ids: list[str] = Field(description="2 to 4 distinct card UUIDs to compare")


class CandidatePatchEdit(BaseModel):
    field_path: str = Field(description="JSON Pointer path to edit")
    value: Any = Field(description="Replacement value")


class CandidatePatchRequest(BaseModel):
    expected_review_revision: int = Field(description="Optimistic locking revision number")
    edits: list[CandidatePatchEdit] = Field(description="List of edits to apply")
    reason: str = Field(description="Reason for patch")


class CandidateDecisionRequest(BaseModel):
    expected_review_revision: int = Field(description="Expected candidate review revision")
    decision: str = Field(description="Decision: 'approve' or 'reject'")
    reason: str = Field(description="Audit explanation for the decision")
    acknowledged_warning_codes: list[str] = Field(default_factory=list, description="Acknowledged extraction warnings")


class ScrapeRunRequest(BaseModel):
    source_id: str = Field(description="Registered source UUID")
    scope: dict[str, Any] = Field(description="Scrape scope definition")
    reason: str = Field(description="Reason: scheduled_refresh, admin_refresh, or parser_recheck")


class ScrapeTriggerRequest(BaseModel):
    source_ids: list[str] | None = Field(default=None, description="Source IDs to scrape")


class DiscoverRequest(BaseModel):
    url: str = Field(description="Target URL to crawl")
    issuer: str | None = Field(default="Discovered Bank", description="Bank/issuer name")
    pattern: str | None = Field(default=None, description="Optional regex pattern to filter links")
    scrape_now: bool | None = Field(default=False, description="Whether to scrape discovered URLs immediately")


class APIException(Exception):
    """Structured RFC-style API exception."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []
        self.headers = headers or {}
        super().__init__(message)


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
    except (LookupError, RuntimeError):
        raise
    except Exception:
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
    <button id="tabSimulateBtn" onclick="showTab('tabSimulate')">Rewards Simulator</button>
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

<!-- Tab 3: Rewards Simulator -->
<section id="tabSimulate" class="tab-content">
  <div class="controls" style="background:var(--surface); padding:20px; border-radius:8px; border:1px solid var(--border); margin-bottom:20px;">
    <div style="margin-bottom:12px;">
      <h2 style="font-size:1.25rem; color:var(--primary); margin-bottom:4px;">Singapore Credit Card Reward Simulator</h2>
      <p style="font-size:0.85rem; color:var(--muted);">Simulate monthly cashback and miles across cards with real Singapore banking rules: minimum spend tiers, monthly caps, and MCC exclusions (government, e-wallets, insurance, utilities).</p>
    </div>

    <div style="display:flex; gap:8px; margin-bottom:16px; flex-wrap:wrap;">
      <span style="font-size:0.8rem; color:var(--muted); align-self:center;">Spending Presets:</span>
      <button type="button" style="padding:4px 10px; font-size:0.8rem;" onclick="setSpendPreset(400, 300, 200, 100, 0)">Young Pro ($1,000)</button>
      <button type="button" style="padding:4px 10px; font-size:0.8rem;" onclick="setSpendPreset(800, 600, 500, 300, 200)">Family ($2,400)</button>
      <button type="button" style="padding:4px 10px; font-size:0.8rem;" onclick="setSpendPreset(150, 150, 100, 0, 0)">Budget ($400)</button>
    </div>

    <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px; margin-bottom:16px;">
      <div>
        <label style="font-size:0.8rem; color:var(--muted); display:block; margin-bottom:4px;">Dining (MCC 5812):</label>
        <input type="number" id="spendDining" value="400" min="0" step="10" style="width:100%;">
      </div>
      <div>
        <label style="font-size:0.8rem; color:var(--muted); display:block; margin-bottom:4px;">Groceries (MCC 5411):</label>
        <input type="number" id="spendGroceries" value="300" min="0" step="10" style="width:100%;">
      </div>
      <div>
        <label style="font-size:0.8rem; color:var(--muted); display:block; margin-bottom:4px;">Shopping / Online (MCC 5311):</label>
        <input type="number" id="spendShopping" value="200" min="0" step="10" style="width:100%;">
      </div>
      <div>
        <label style="font-size:0.8rem; color:var(--muted); display:block; margin-bottom:4px;">Utilities / Power (MCC 4900 - Excl):</label>
        <input type="number" id="spendUtilities" value="100" min="0" step="10" style="width:100%;">
      </div>
      <div>
        <label style="font-size:0.8rem; color:var(--muted); display:block; margin-bottom:4px;">Other Retail (General):</label>
        <input type="number" id="spendGeneral" value="0" min="0" step="10" style="width:100%;">
      </div>
    </div>

    <button class="btn-primary" style="padding:10px 24px; font-size:0.95rem;" onclick="runSimulation()">Calculate & Rank Cards</button>
  </div>

  <div id="simulationResults"></div>
</section>

<!-- Tab 4: Admin Review Dashboard & Crawler Controls -->
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
  if (tabId === 'tabSimulate') {
    document.getElementById('tabSimulateBtn').classList.add('active');
    runSimulation();
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

function setSpendPreset(dining, groceries, shopping, utilities, general) {
  document.getElementById('spendDining').value = dining;
  document.getElementById('spendGroceries').value = groceries;
  document.getElementById('spendShopping').value = shopping;
  document.getElementById('spendUtilities').value = utilities;
  document.getElementById('spendGeneral').value = general;
  runSimulation();
}

async function runSimulation() {
  const dining = parseFloat(document.getElementById('spendDining').value) || 0;
  const groceries = parseFloat(document.getElementById('spendGroceries').value) || 0;
  const shopping = parseFloat(document.getElementById('spendShopping').value) || 0;
  const utilities = parseFloat(document.getElementById('spendUtilities').value) || 0;
  const general = parseFloat(document.getElementById('spendGeneral').value) || 0;

  const spendList = [];
  if (dining > 0) spendList.push({ amount: dining.toFixed(2), category: 'dining', mcc: '5812', description: 'Dining & Cafes' });
  if (groceries > 0) spendList.push({ amount: groceries.toFixed(2), category: 'groceries', mcc: '5411', description: 'Supermarket Groceries' });
  if (shopping > 0) spendList.push({ amount: shopping.toFixed(2), category: 'shopping', mcc: '5311', description: 'Online & Retail Shopping' });
  if (utilities > 0) spendList.push({ amount: utilities.toFixed(2), category: 'utilities', mcc: '4900', description: 'Electricity & Water Bill' });
  if (general > 0) spendList.push({ amount: general.toFixed(2), category: 'general', mcc: '5999', description: 'General Retail Spend' });

  if (!spendList.length) {
    document.getElementById('simulationResults').innerHTML = '<p style="color:var(--warning)">Please enter at least one positive monthly spend amount.</p>';
    return;
  }

  document.getElementById('simulationResults').innerHTML = '<p style="color:var(--muted)">Calculating rewards across catalogue...</p>';

  try {
    const res = await fetch('/api/v1/cards/simulate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ monthly_spend: spendList })
    });
    const data = await res.json();
    if (!res.ok) {
      document.getElementById('simulationResults').innerHTML = `<p style="color:var(--danger)">Simulation failed: ${data.error ? data.error.message : 'Unknown error'}</p>`;
      return;
    }
    renderSimulationResults(data);
  } catch (err) {
    document.getElementById('simulationResults').innerHTML = '<p style="color:var(--danger)">Failed to connect to simulation engine.</p>';
  }
}

function renderSimulationResults(data) {
  const container = document.getElementById('simulationResults');
  if (!data.ranked_cards || !data.ranked_cards.length) {
    container.innerHTML = '<p style="color:var(--muted)">No published cards available for simulation.</p>';
    return;
  }

  let html = `<div style="margin-bottom:16px; font-size:0.95rem; display:flex; gap:16px; align-items:center;">
    <span>Total Spend: <strong>S$${data.total_monthly_spend}</strong></span>
    <span>Transactions: <strong>${data.transactions_count}</strong></span>
  </div>`;

  html += `<div style="display:flex; flex-direction:column; gap:16px;">`;
  data.ranked_cards.forEach(card => {
    const isTop = card.rank === 1;
    const badgeColor = isTop ? 'var(--accent)' : 'var(--border)';
    const minSpendTag = card.minimum_spend_met
      ? `<span class="badge badge-approved" style="margin-left:8px;">Min Spend Met (S$${card.minimum_monthly_spend || '0.00'})</span>`
      : `<span class="badge badge-pending" style="margin-left:8px;">Min Spend Not Met (Requires S$${card.minimum_monthly_spend})</span>`;

    html += `
      <div style="background:var(--card-bg); border: 2px solid ${badgeColor}; border-radius:8px; padding:16px;">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px;">
          <div>
            <div style="display:flex; align-items:center; gap:8px;">
              <span style="font-size:1.2rem; font-weight:700; color:var(--primary);">#${card.rank}</span>
              <h3 style="font-size:1.15rem; color:#fff;">${escapeHtml(card.card_name)}</h3>
              <span style="font-size:0.85rem; color:var(--muted);">(${escapeHtml(card.issuer)})</span>
              ${minSpendTag}
            </div>
            <p style="font-size:0.85rem; color:var(--muted); margin-top:4px;">Effective Reward Rate: <strong style="color:var(--accent);">${card.effective_reward_rate}</strong></p>
          </div>
          <div style="text-align:right;">
            <div style="font-size:1.5rem; font-weight:700; color:var(--accent);">S$${card.total_reward_earned}</div>
            <div style="font-size:0.75rem; color:var(--muted); text-transform:uppercase;">Estimated Monthly ${card.reward_type}</div>
          </div>
        </div>

        <details style="margin-top:12px; cursor:pointer;">
          <summary style="font-size:0.85rem; color:var(--primary);">View Transaction Breakdown (${card.breakdown.length} items)</summary>
          <table>
            <thead>
              <tr>
                <th>Category</th>
                <th>Spend</th>
                <th>MCC</th>
                <th>Applied Rate</th>
                <th>Reward Earned</th>
                <th>Calculation Rule / Cap Status</th>
              </tr>
            </thead>
            <tbody>
              ${card.breakdown.map(b => `
                <tr style="${b.status === 'excluded' ? 'opacity:0.6;' : ''}">
                  <td>${escapeHtml(b.description)}</td>
                  <td>S$${b.amount}</td>
                  <td>${b.mcc || '-'}</td>
                  <td>${(parseFloat(b.rate)*100).toFixed(2)}%</td>
                  <td><strong>S$${b.reward_earned}</strong></td>
                  <td style="font-size:0.8rem; color:${b.status === 'excluded' ? 'var(--danger)' : 'var(--muted)'};">${escapeHtml(b.reason)}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </details>
      </div>
    `;
  });
  html += `</div>`;

  container.innerHTML = html;
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


def create_app(
    config: dict,
    db_path: str,
    api_key: str | None = None,
    allow_private_hosts: bool = False,
    *,
    admin_secret: str | None = None,
    internal_secret: str | None = None,
) -> FastAPI:
    """FastAPI application factory for the credit card catalogue and review service."""
    if api_key == "":
        raise ValueError("api_key must be nonempty when supplied")

    admin_sec = admin_secret or api_key or os.getenv("CARD_ADMIN_BEARER")
    internal_sec = internal_secret or os.getenv("CARD_INTERNAL_BEARER")
    cursor_secret = os.getenv("CARD_CURSOR_HMAC_SECRET") or admin_sec or "local-development-cursor-key"

    app = FastAPI(
        title="Card Catalogue & Ingestion Service",
        version="1.0.0",
        description="Type-safe FastAPI catalogue and recommendation backend with OpenAPI/Swagger interactive documentation.",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        incoming_rid = request.headers.get("X-Request-ID", "")
        rid = incoming_rid if _uuid(incoming_rid) else str(uuid.uuid4())
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(APIException)
    async def handle_api_exception(request: Request, exc: APIException):
        rid = getattr(request.state, "request_id", str(uuid.uuid4()))
        headers = dict(exc.headers)
        headers["X-Request-ID"] = rid
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                    "request_id": rid,
                }
            },
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        rid = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Validation error.",
                    "details": exc.errors(),
                    "request_id": rid,
                }
            },
            headers={"X-Request-ID": rid},
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException):
        rid = getattr(request.state, "request_id", str(uuid.uuid4()))
        code = "NOT_FOUND" if exc.status_code == 404 else ("UNAUTHENTICATED" if exc.status_code == 401 else "ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code,
                    "message": "Route not found." if exc.status_code == 404 else str(exc.detail),
                    "details": [],
                    "request_id": rid,
                }
            },
            headers={"X-Request-ID": rid},
        )

    @app.exception_handler(Exception)
    async def handle_generic_exception(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", str(uuid.uuid4()))
        path = request.url.path
        if path in {"/v1/cards", "/v1/scrape-runs"} or path.startswith("/v1/cards/"):
            msg = "database query failed"
        else:
            msg = "Request could not be completed."
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": msg,
                    "details": [],
                    "request_id": rid,
                }
            },
            headers={"X-Request-ID": rid},
        )

    def check_auth(request: Request, kind: str) -> None:
        secret = admin_sec if kind == "admin" else internal_sec
        if not secret:
            raise APIException(503, "SERVICE_UNAVAILABLE", "Required server identity is not configured.")
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        if not hmac.compare_digest(token, secret):
            raise APIException(401, "UNAUTHENTICATED", "Missing or invalid bearer token.")

    def check_auth_bool(request: Request, kind: str) -> bool:
        secret = admin_sec if kind == "admin" else internal_sec
        if not secret:
            return False
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        return bool(hmac.compare_digest(token, secret))

    async def get_body(request: Request, required: set[str], optional: set[str] = set()) -> dict:
        content_type = request.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            raise APIException(415, "UNSUPPORTED_MEDIA_TYPE", "JSON Content-Type is required.")
        try:
            raw = await request.body()
            if len(raw) > MAX_BODY:
                raise ValueError
            data = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(data, dict):
                raise TypeError
            keys = set(data.keys())
            if (keys - required - optional) or not (required <= keys):
                raise TypeError
            return data
        except (ValueError, TypeError, json.JSONDecodeError):
            raise APIException(400, "MALFORMED_REQUEST", "Malformed JSON request.")

    def paginate(items: list, filters: dict, revision: int, limit_raw: int | str, cursor_token: str | None = None) -> dict:
        try:
            limit = int(limit_raw)
            if not (1 <= limit <= 100):
                raise ValueError
        except (ValueError, TypeError):
            raise APIException(422, "VALIDATION_ERROR", "limit must be an integer from 1 to 100.")

        try:
            offset = _decode_cursor(cursor_secret, cursor_token, filters, revision) if cursor_token else 0
        except LookupError:
            raise APIException(422, "CURSOR_FILTER_MISMATCH", "Cursor is bound to different filters.")
        except RuntimeError:
            raise APIException(409, "CURSOR_EXPIRED", "Cursor expired or catalogue changed.")
        except ValueError:
            raise APIException(422, "VALIDATION_ERROR", "Invalid cursor.")

        selected = items[offset : offset + limit]
        next_cursor = (
            _cursor(cursor_secret, filters, revision, offset + limit)
            if offset + limit < len(items)
            else None
        )
        return {"items": selected, "next_cursor": next_cursor}

    # UI and Health
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def serve_dashboard():
        return HTMLResponse(content=DASHBOARD_HTML)

    @app.get("/health", tags=["System"])
    def health_check():
        db.initialize(db_path)
        return {"status": "ok", "database": "ready"}

    @app.get("/metrics", tags=["System"])
    def get_metrics():
        return {
            "service": "card_catalogue",
            "mode": "sqlite" if not db.is_mysql(db_path) else "mysql",
            "note": "Prometheus text is available at /metrics/prometheus",
        }

    @app.get("/metrics/prometheus", response_class=PlainTextResponse, tags=["System"])
    def get_prometheus_metrics():
        return PlainTextResponse("", media_type="text/plain; version=0.0.4", headers={"Cache-Control": "no-store"})

    # Legacy routes
    @app.get("/v1/cards", tags=["Legacy"])
    def legacy_cards(request: Request, issuer: str | None = None, source_id: str | None = None):
        return {"cards": db.cards(db_path, issuer, source_id)}

    @app.get("/v1/cards/{card_id}", tags=["Legacy"])
    def legacy_card(card_id: str):
        cid = unquote(card_id)
        item = db.card(db_path, cid)
        if item:
            return item
        raise APIException(404, "NOT_FOUND", "Card not found.")

    @app.get("/v1/scrape-runs", tags=["Legacy"])
    def legacy_runs(limit: int = 50):
        return {"scrape_runs": db.runs(db_path, limit)}

    @app.post("/v1/scrape", tags=["Legacy"])
    @app.post("/api/v1/admin/scrape", tags=["Admin"])
    async def legacy_or_admin_scrape(request: Request):
        if (admin_sec or api_key) and not check_auth_bool(request, "admin"):
            raise APIException(401, "UNAUTHENTICATED", "Missing or invalid bearer token.")
        body = await get_body(request, set(), {"source_ids"})
        ids = body.get("source_ids")
        if ids is not None and (not isinstance(ids, list) or not all(isinstance(x, str) for x in ids)):
            raise APIException(400, "MALFORMED_REQUEST", "Invalid scrape request: source_ids must be list of strings.")
        try:
            results = scrape_sources(selected_sources(config, ids if ids else None), db_path, allow_private_hosts)
            return {"status": "completed", "results": results}
        except Exception as exc:
            raise APIException(400, "MALFORMED_REQUEST", f"Invalid scrape request: {exc}")

    # Public Catalogue routes
    @app.get("/api/v1/cards", tags=["Catalogue"])
    def list_cards(
        request: Request,
        q: str | None = None,
        bank_id: str | None = None,
        reward_type: str | None = None,
        simulation_support: str | None = None,
        freshness: str | None = None,
        limit: str = "20",
        cursor: str | None = None,
    ):
        filters = {}
        if q is not None: filters["q"] = q
        if bank_id is not None: filters["bank_id"] = bank_id
        if reward_type is not None: filters["reward_type"] = reward_type
        if simulation_support is not None: filters["simulation_support"] = simulation_support
        if freshness is not None: filters["freshness"] = freshness

        if filters.get("bank_id") and not _uuid(filters["bank_id"]):
            raise APIException(422, "VALIDATION_ERROR", "bank_id must be UUID.")

        if filters.get("q") and len(filters["q"]) > 100:
            raise APIException(422, "VALIDATION_ERROR", "q is too long.")

        valid_enums = {
            "reward_type": {"cashback", "miles", "points", "mixed"},
            "simulation_support": {"supported", "unsupported"},
            "freshness": {"fresh", "stale"},
        }
        if any(filters.get(k) not in valid_enums[k] for k in valid_enums if filters.get(k)):
            raise APIException(422, "VALIDATION_ERROR", "Invalid filter enum.")

        rev, items = db.published_cards(db_path, filters)
        tag = _etag(rev, "cards:" + json.dumps(filters, sort_keys=True))
        if request.headers.get("If-None-Match") == tag:
            rid = getattr(request.state, "request_id", str(uuid.uuid4()))
            return Response(status_code=304, headers={"ETag": tag, "X-Request-ID": rid})

        page = paginate(items, filters, rev, limit, cursor)
        rid = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=200,
            content={"catalogue_revision": rev, **page},
            headers={"ETag": tag, "X-Request-ID": rid},
        )

    @app.get("/api/v1/cards/{card_id}", tags=["Catalogue"])
    def get_card_detail(request: Request, card_id: str):
        cid = unquote(card_id)
        if not _uuid(cid):
            raise APIException(422, "VALIDATION_ERROR", "card_id must be UUID.")

        detail = db.current_detail(db_path, cid)
        if not detail:
            raise APIException(404, "NOT_FOUND", "Card is unavailable.")

        tag = _etag(detail["catalogue_revision"], "detail:" + cid)
        if request.headers.get("If-None-Match") == tag:
            rid = getattr(request.state, "request_id", str(uuid.uuid4()))
            return Response(status_code=304, headers={"ETag": tag, "X-Request-ID": rid})

        rid = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=200,
            content={
                "catalogue_revision": detail["catalogue_revision"],
                "card": detail["card"],
                "terms": detail["terms"],
                "effective_from": detail["effective_from"],
                "effective_to": detail["effective_to"],
                "evidence": detail["evidence"],
            },
            headers={"ETag": tag, "X-Request-ID": rid},
        )

    @app.post("/api/v1/cards/compare", tags=["Catalogue"])
    async def compare_cards_endpoint(request: Request):
        body = await get_body(request, {"card_ids"})
        ids = body.get("card_ids")
        if (
            not isinstance(ids, list)
            or not (2 <= len(ids) <= 4)
            or len(set(ids)) != len(ids)
            or not all(_uuid(x) for x in ids)
        ):
            raise APIException(422, "VALIDATION_ERROR", "card_ids must contain 2-4 distinct UUIDs.")

        cards_detail = [db.current_detail(db_path, x) for x in ids]
        if any(x is None for x in cards_detail):
            raise APIException(404, "NOT_FOUND", "A requested card is unavailable.")

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

        return {
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
        }

    @app.post("/api/v1/cards/simulate", tags=["Catalogue"])
    async def simulate_rewards_endpoint(request: Request):
        body = await get_body(request, {"monthly_spend"}, {"card_ids"})
        spend = body.get("monthly_spend")
        if not isinstance(spend, list) or not (1 <= len(spend) <= 50):
            raise APIException(422, "VALIDATION_ERROR", "monthly_spend must be a list of 1 to 50 items.")
        for item in spend:
            if not isinstance(item, dict) or "amount" not in item:
                raise APIException(422, "VALIDATION_ERROR", "Each spend item must include 'amount'.")
            try:
                amt = float(item["amount"])
                if amt <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                raise APIException(422, "VALIDATION_ERROR", "Spend 'amount' must be a positive number.")

        c_ids = body.get("card_ids")
        if c_ids is not None:
            if not isinstance(c_ids, list) or not all(_uuid(x) for x in c_ids):
                raise APIException(422, "VALIDATION_ERROR", "card_ids must be a list of valid UUIDs.")

        return db.simulate_rewards_all(db_path, c_ids, spend)

    # Internal Recommendation routes
    @app.get("/api/v1/internal/catalogue-snapshots/current", tags=["Internal"])
    def get_internal_snapshot(
        request: Request,
        schema_version: str = "card_terms.v1",
        simulation_support: str = "supported",
        market: str = "SG",
        reward_type: str | None = None,
    ):
        check_auth(request, "internal")
        if schema_version != "card_terms.v1" or simulation_support not in {"supported", "all"}:
            raise APIException(422, "UNSUPPORTED_SCHEMA_VERSION", "Unsupported schema or support filter.")

        filter_arg = {"reward_type": reward_type} if reward_type else {}
        rev, items = db.published_cards(db_path, filter_arg)
        details = [
            db.current_detail(db_path, x["card_id"])
            for x in items
            if simulation_support == "all" or x["simulation_support"] == "supported"
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
        return {
            "schema_version": "card_catalogue_snapshot.v1",
            "card_terms_schema_version": "card_terms.v1",
            "catalogue_revision": rev,
            "generated_at": utc_now(),
            "cards": cards_list,
        }

    @app.get("/api/v1/internal/card-versions/{card_version_id}", tags=["Internal"])
    def get_internal_card_version(request: Request, card_version_id: str):
        check_auth(request, "internal")
        vid = unquote(card_version_id)
        if not _uuid(vid):
            raise APIException(422, "VALIDATION_ERROR", "card_version_id must be UUID.")

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
            raise APIException(404, "NOT_FOUND", "Version unavailable.")

        rid = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=200,
            content={
                "published_in_catalogue_revision": row["catalogue_revision"],
                "superseded_in_catalogue_revision": row["superseded_revision"],
                "card": json.loads(row["summary_json"]),
                "terms": json.loads(row["terms_json"]),
                "effective_from": row["effective_from"],
                "effective_to": row["effective_to"],
                "evidence": evidence,
            },
            headers={"Cache-Control": "no-store", "X-Request-ID": rid},
        )

    # Admin Management routes
    @app.get("/api/v1/admin/config-sources", tags=["Admin"])
    def get_admin_config_sources(request: Request):
        check_auth(request, "admin")
        rid = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=200,
            content={"sources": config.get("sources", [])},
            headers={"Cache-Control": "no-store", "X-Request-ID": rid},
        )

    @app.get("/api/v1/admin/sources", tags=["Admin"])
    def list_admin_sources(
        request: Request,
        kind: str | None = None,
        access_review_status: str | None = None,
        freshness: str | None = None,
        limit: str = "20",
        cursor: str | None = None,
    ):
        check_auth(request, "admin")
        rows = db.source_list(db_path)
        filters = {}
        if kind is not None: filters["kind"] = kind
        if access_review_status is not None: filters["access_review_status"] = access_review_status
        if freshness is not None: filters["freshness"] = freshness

        filtered = [row for row in rows if all(str(row.get(k)) == str(v) for k, v in filters.items())]
        page = paginate(filtered, filters, 0, limit, cursor)
        rid = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(status_code=200, content=page, headers={"Cache-Control": "no-store", "X-Request-ID": rid})

    @app.post("/api/v1/admin/discover", tags=["Admin"])
    @app.post("/api/v1/admin/sources/discover", tags=["Admin"])
    async def admin_discover_links(request: Request):
        check_auth(request, "admin")
        body = await get_body(request, {"url"}, {"issuer", "pattern", "scrape_now"})
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
            rid = getattr(request.state, "request_id", str(uuid.uuid4()))
            return JSONResponse(
                status_code=200,
                content={
                    "status": "success",
                    "discovered_count": len(discovered),
                    "cards": discovered,
                    "scraped_count": len(scraped_results),
                    "results": scraped_results,
                },
                headers={"Cache-Control": "no-store", "X-Request-ID": rid},
            )
        except Exception as exc:
            raise APIException(400, "DISCOVERY_FAILED", str(exc))

    @app.post("/api/v1/admin/scrape-runs", tags=["Admin"])
    async def admin_queue_scrape_run(request: Request):
        check_auth(request, "admin")
        key = request.headers.get("Idempotency-Key", "")
        if not key or len(key) > 255:
            raise APIException(422, "VALIDATION_ERROR", "Idempotency-Key is required.")

        body = await get_body(request, {"source_id", "scope", "reason"})
        if (
            not _uuid(body.get("source_id"))
            or body.get("reason") not in {"scheduled_refresh", "admin_refresh", "parser_recheck"}
            or not isinstance(body.get("scope"), dict)
            or body.get("scope", {}).get("type") not in {"all", "documents"}
        ):
            raise APIException(422, "VALIDATION_ERROR", "Invalid registered source or scope.")

        body_hash = hashlib.sha256(_json(body)).hexdigest()
        try:
            cached = db.idempotency_get(db_path, "admin", request.url.path, key, body_hash)
            if cached:
                rid = getattr(request.state, "request_id", str(uuid.uuid4()))
                headers = dict(cached[1])
                headers["X-Request-ID"] = rid
                return JSONResponse(status_code=cached[0], content=cached[2], headers=headers)

            result = db.queue_run(db_path, body["source_id"], body["scope"], body["reason"])
            headers = {"Cache-Control": "no-store"}
            db.idempotency_put(db_path, "admin", request.url.path, key, body_hash, 202, headers, result)
            rid = getattr(request.state, "request_id", str(uuid.uuid4()))
            headers["X-Request-ID"] = rid
            return JSONResponse(status_code=202, content=result, headers=headers)
        except RuntimeError as exc:
            err_msg = str(exc)
            code = "IDEMPOTENCY_CONFLICT" if err_msg == "IDEMPOTENCY_CONFLICT" else "SOURCE_ALREADY_RUNNING"
            raise APIException(409, code, "Request conflicts with existing state.", [{"run_id": err_msg.split(":")[-1]}])
        except ValueError:
            raise APIException(422, "VALIDATION_ERROR", "Invalid registered source or scope.")

    @app.get("/api/v1/admin/scrape-runs/{run_id}", tags=["Admin"])
    def admin_view_scrape_run(request: Request, run_id: str):
        check_auth(request, "admin")
        rid = unquote(run_id)
        if not _uuid(rid):
            raise APIException(422, "VALIDATION_ERROR", "run_id must be UUID.")
        item = db.run(db_path, rid)
        if not item:
            raise APIException(404, "NOT_FOUND", "Run unavailable.")
        req_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(status_code=200, content=item, headers={"Cache-Control": "no-store", "X-Request-ID": req_id})

    @app.get("/api/v1/admin/extraction-candidates", tags=["Admin"])
    def admin_list_candidates(
        request: Request,
        review_status: str | None = None,
        source_id: str | None = None,
        scrape_run_id: str | None = None,
        bank_id: str | None = None,
        material_change: str | None = None,
        limit: str = "10",
        cursor: str | None = None,
    ):
        check_auth(request, "admin")
        filters = {}
        if review_status is not None: filters["review_status"] = review_status
        if source_id is not None: filters["source_id"] = source_id
        if scrape_run_id is not None: filters["scrape_run_id"] = scrape_run_id
        if bank_id is not None: filters["bank_id"] = bank_id
        if material_change is not None: filters["material_change"] = (material_change == "true")

        items = db.candidate_list(db_path, filters)
        page = paginate(items, filters, 0, limit, cursor)
        req_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(status_code=200, content=page, headers={"Cache-Control": "no-store", "X-Request-ID": req_id})

    @app.get("/api/v1/admin/extraction-candidates/{candidate_id}", tags=["Admin"])
    def admin_view_candidate(request: Request, candidate_id: str):
        check_auth(request, "admin")
        cid = unquote(candidate_id)
        if not _uuid(cid):
            raise APIException(422, "VALIDATION_ERROR", "candidate_id must be UUID.")
        cand = db.get_candidate(db_path, cid)
        if not cand:
            raise APIException(404, "NOT_FOUND", "Candidate unavailable.")
        req_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(status_code=200, content=cand, headers={"Cache-Control": "no-store", "X-Request-ID": req_id})

    @app.patch("/api/v1/admin/extraction-candidates/{candidate_id}", tags=["Admin"])
    async def admin_update_candidate(request: Request, candidate_id: str):
        check_auth(request, "admin")
        cid = unquote(candidate_id)
        if not _uuid(cid):
            raise APIException(422, "VALIDATION_ERROR", "candidate_id must be UUID.")
        body = await get_body(request, {"expected_review_revision", "edits", "reason"})
        if (
            not isinstance(body.get("expected_review_revision"), int)
            or not isinstance(body.get("edits"), list)
            or not (1 <= len(body["edits"]) <= 50)
            or not isinstance(body.get("reason"), str)
            or any(not isinstance(x, dict) or set(x.keys()) != {"field_path", "value"} for x in body["edits"])
        ):
            raise APIException(422, "VALIDATION_ERROR", "Invalid candidate patch.")

        try:
            updated = db.patch_candidate(
                db_path,
                cid,
                body["expected_review_revision"],
                body["edits"],
                body["reason"],
                "admin",
            )
            req_id = getattr(request.state, "request_id", str(uuid.uuid4()))
            return JSONResponse(status_code=200, content=updated, headers={"Cache-Control": "no-store", "X-Request-ID": req_id})
        except LookupError:
            raise APIException(404, "NOT_FOUND", "Candidate unavailable.")
        except RuntimeError as exc:
            raise APIException(409, str(exc).split(":")[0], "Review state changed.")
        except ValueError:
            raise APIException(422, "VALIDATION_ERROR", "Invalid candidate edits.")

    @app.post("/api/v1/admin/extraction-candidates/{candidate_id}/decisions", tags=["Admin"])
    async def admin_decide_candidate_endpoint(request: Request, candidate_id: str):
        check_auth(request, "admin")
        cid = unquote(candidate_id)
        key = request.headers.get("Idempotency-Key", "")
        if not key or not _uuid(cid):
            raise APIException(422, "VALIDATION_ERROR", "Idempotency-Key and valid candidate_id are required.")

        body = await get_body(request, {"expected_review_revision", "decision", "reason", "acknowledged_warning_codes"})
        if (
            body.get("decision") not in {"approve", "reject"}
            or not isinstance(body.get("expected_review_revision"), int)
            or not isinstance(body.get("reason"), str)
            or not (1 <= len(body["reason"]) <= 1000)
            or not isinstance(body.get("acknowledged_warning_codes"), list)
        ):
            raise APIException(422, "VALIDATION_ERROR", "Invalid decision request.")

        body_hash = hashlib.sha256(_json(body)).hexdigest()
        try:
            cached = db.idempotency_get(db_path, "admin", request.url.path, key, body_hash)
            if cached:
                req_id = getattr(request.state, "request_id", str(uuid.uuid4()))
                headers = dict(cached[1])
                headers["X-Request-ID"] = req_id
                return JSONResponse(status_code=cached[0], content=cached[2], headers=headers)

            status, result = db.decide(
                db_path,
                cid,
                body["expected_review_revision"],
                body["decision"],
                body["reason"],
                body["acknowledged_warning_codes"],
                "admin",
            )
            headers = {"Cache-Control": "no-store"}
            db.idempotency_put(db_path, "admin", request.url.path, key, body_hash, status, headers, result)
            req_id = getattr(request.state, "request_id", str(uuid.uuid4()))
            headers["X-Request-ID"] = req_id
            return JSONResponse(status_code=status, content=result, headers=headers)
        except LookupError:
            raise APIException(404, "NOT_FOUND", "Candidate unavailable.")
        except RuntimeError as exc:
            raise APIException(409, str(exc).split(":")[0], "Review state changed.")
        except ValueError:
            raise APIException(422, "PUBLICATION_VALIDATION_FAILED", "Candidate cannot be published.")

    return app


def make_handler(
    config: dict,
    db_path: str,
    api_key: str | None,
    allow_private_hosts: bool,
    *,
    admin_secret: str | None = None,
    internal_secret: str | None = None,
):
    """Factory for HTTP request handler adapting FastAPI to standard ThreadingHTTPServer."""
    if api_key == "":
        raise ValueError("api_key must be nonempty when supplied")

    app = create_app(
        config,
        db_path,
        api_key,
        allow_private_hosts,
        admin_secret=admin_secret,
        internal_secret=internal_secret,
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "CardCatalogue/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _handle_request(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query_string = parsed.query.encode("latin1")
            content_length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(content_length) if content_length > 0 else b""

            headers = [
                (k.lower().encode("latin1"), v.encode("latin1"))
                for k, v in self.headers.items()
            ]

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": self.command,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("latin1"),
                "query_string": query_string,
                "headers": headers,
                "client": self.client_address,
                "server": self.server.server_address,
            }

            body_sent = False

            async def receive() -> dict[str, Any]:
                nonlocal body_sent
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            headers_sent = False

            HEADER_CANONICAL = {
                "content-type": "Content-Type",
                "content-length": "Content-Length",
                "etag": "ETag",
                "x-request-id": "X-Request-ID",
                "cache-control": "Cache-Control",
                "idempotency-key": "Idempotency-Key",
                "authorization": "Authorization",
                "location": "Location",
            }

            async def send(message: dict[str, Any]) -> None:
                nonlocal headers_sent
                if message["type"] == "http.response.start":
                    self.send_response(message["status"])
                    for hk, hv in message.get("headers", []):
                        k_str = hk.decode("latin1").lower()
                        k_canon = HEADER_CANONICAL.get(k_str) or "-".join(p.capitalize() for p in k_str.split("-"))
                        self.send_header(k_canon, hv.decode("latin1"))
                    self.end_headers()
                    headers_sent = True
                elif message["type"] == "http.response.body":
                    if not headers_sent:
                        self.send_response(200)
                        self.end_headers()
                        headers_sent = True
                    chunk = message.get("body", b"")
                    if chunk:
                        self.wfile.write(chunk)

            try:
                asyncio.run(app(scope, receive, send))
            except Exception:
                if not headers_sent:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"code":"INTERNAL_ERROR","message":"Request could not be completed."}}')

        do_GET = _handle_request
        do_POST = _handle_request
        do_PATCH = _handle_request
        do_DELETE = _handle_request
        do_HEAD = _handle_request
        do_OPTIONS = _handle_request

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
    """Start high-performance Uvicorn server hosting the FastAPI credit card catalogue application."""
    db.initialize(db_path)
    app = create_app(
        config,
        db_path,
        api_key,
        allow_private_hosts,
        admin_secret=admin_secret,
        internal_secret=internal_secret,
    )
    import uvicorn
    uvicorn.run(app, host=host, port=port, access_log=False)
