import os
import json
import sys
from aiohttp import web

class DashboardState:
    def __init__(self):
        self.stage = "Initializing"
        self.articles_fetched = 0
        self.articles_summarized = 0
        self.ideas_extracted = 0
        self.ideas_scored = 0
        self.podcasts_transcribed = 0
        
        self.transcription_progress = 0  # 0-100
        self.transcription_current_podcast = ""
        
        # Current Stage Progress
        self.current_item_index = 0
        self.total_items = 0
        
        # Sub-task progress (e.g. current podcast, current LLM generation)
        self.current_task = ""
        self.task_progress = 0  # 0-100
        self.eta_seconds = 0
        self.start_time = 0
        self.task_start_time = 0
        
        # Live LLM Monitor
        self.llm_model = ""
        self.llm_prompt = ""
        self.llm_thought = ""
        self.llm_response = ""
        
        self.ideas_data = []      # list of dicts (Stage 1)
        self.plans_data = []      # list of dicts (Stage 2)
        self.reports_data = []    # list of dicts (Stage 3)
        self.actions_data = []    # list of dicts (Stage 4)

state = DashboardState()

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GeoPoTech | Trading Intelligence</title>
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=Playfair+Display:wght@700;800&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg:        #080807;
            --bg-2:      #0f0f0d;
            --bg-3:      #161613;
            --amber:     #d4930a;
            --amber-dim: rgba(212, 147, 10, 0.12);
            --amber-glow:rgba(212, 147, 10, 0.25);
            --green:     #52a882;
            --green-dim: rgba(82, 168, 130, 0.12);
            --red:       #c0504a;
            --red-dim:   rgba(192, 80, 74, 0.12);
            --yellow:    #c8a84b;
            --yellow-dim:rgba(200, 168, 75, 0.12);
            --text:      #e8e4d8;
            --text-2:    #9e9a8e;
            --text-3:    #5a5750;
            --line:      rgba(255,255,255,0.06);
            --line-2:    rgba(255,255,255,0.03);
        }

        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: 'IBM Plex Sans', sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
            /* Subtle scanline texture */
            background-image:
                repeating-linear-gradient(
                    0deg,
                    transparent,
                    transparent 2px,
                    rgba(0,0,0,0.04) 2px,
                    rgba(0,0,0,0.04) 4px
                );
        }

        /* ── Scrollbar ── */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #2a2920; border-radius: 3px; }

        /* ── Layout Shell ── */
        .shell {
            display: grid;
            grid-template-rows: auto auto 1fr;
            min-height: 100vh;
        }

        /* ── Top Bar ── */
        .topbar {
            display: flex;
            align-items: stretch;
            border-bottom: 1px solid var(--line);
            height: 56px;
        }

        .topbar-brand {
            display: flex;
            align-items: center;
            gap: 14px;
            padding: 0 28px;
            border-right: 1px solid var(--line);
        }

        .brand-mark {
            width: 30px;
            height: 30px;
            border: 1.5px solid var(--amber);
            display: flex;
            align-items: center;
            justify-content: center;
            font-family: 'Playfair Display', serif;
            font-size: 0.95rem;
            font-weight: 800;
            color: var(--amber);
            letter-spacing: -1px;
            box-shadow: 0 0 12px var(--amber-glow), inset 0 0 8px rgba(212,147,10,0.05);
        }

        .brand-name {
            font-family: 'Playfair Display', serif;
            font-size: 1.05rem;
            font-weight: 700;
            color: var(--text);
            letter-spacing: 0.5px;
        }

        .brand-sub {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.6rem;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-top: 1px;
        }

        .topbar-status {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 0 24px;
            border-right: 1px solid var(--line);
        }

        .status-pip {
            width: 6px;
            height: 6px;
            background: var(--amber);
            border-radius: 50%;
            box-shadow: 0 0 8px var(--amber);
            animation: blink 2.5s ease-in-out infinite;
            flex-shrink: 0;
        }

        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }

        #stage-text {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.72rem;
            color: var(--amber);
            text-transform: uppercase;
            letter-spacing: 1.5px;
        }

        .topbar-progress {
            display: flex;
            align-items: center;
            gap: 16px;
            padding: 0 24px;
            flex: 1;
        }

        .prog-track {
            flex: 1;
            height: 2px;
            background: var(--line);
            position: relative;
            max-width: 320px;
        }

        .prog-fill {
            height: 100%;
            background: linear-gradient(to right, var(--amber), #f0c040);
            width: 0%;
            transition: width 0.6s cubic-bezier(0.4,0,0.2,1);
            box-shadow: 0 0 8px var(--amber-glow);
        }

        #stage-progress-text {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.65rem;
            color: var(--text-3);
            white-space: nowrap;
        }

        .topbar-right {
            display: flex;
            align-items: center;
            padding: 0 24px;
            margin-left: auto;
            gap: 8px;
            border-left: 1px solid var(--line);
        }

        #model-name {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.65rem;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        /* ── Task Bar ── */
        #task-monitor {
            display: none;
            align-items: center;
            gap: 20px;
            padding: 10px 28px;
            background: var(--amber-dim);
            border-bottom: 1px solid rgba(212,147,10,0.2);
        }

        #current-task-name {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.68rem;
            color: var(--amber);
            text-transform: uppercase;
            letter-spacing: 1.5px;
            white-space: nowrap;
        }

        .task-track {
            flex: 1;
            height: 2px;
            background: rgba(212,147,10,0.15);
        }

        #task-progress-bar {
            height: 100%;
            background: var(--amber);
            width: 0%;
            transition: width 0.4s ease;
            box-shadow: 0 0 6px var(--amber-glow);
        }

        #eta-display {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.65rem;
            color: var(--text-3);
            white-space: nowrap;
        }

        /* ── Main Grid ── */
        .main {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            grid-template-rows: auto 330px 1fr;
            gap: 0;
            border-left: 1px solid var(--line);
        }

        /* ── Stat Strip ── */
        .stat-strip {
            grid-column: 1 / -1;
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            border-bottom: 1px solid var(--line);
        }

        .stat-cell {
            padding: 10px 28px;
            border-right: 1px solid var(--line);
            position: relative;
            overflow: hidden;
        }

        .stat-cell:last-child { border-right: none; }

        .stat-cell::before {
            content: '';
            position: absolute;
            bottom: 0; left: 28px;
            width: 24px; height: 1px;
            background: var(--amber);
        }

        .stat-label {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.6rem;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-bottom: 8px;
        }

        .stat-value {
            font-family: 'Playfair Display', serif;
            font-size: 2.2rem;
            font-weight: 700;
            color: var(--text);
            line-height: 1;
        }

        /* ── Panel Base ── */
        .panel {
            border-right: 1px solid var(--line);
            border-bottom: 1px solid var(--line);
            display: flex;
            flex-direction: column;
            min-height: 0;
            overflow: hidden;
        }

        .panel:last-child,
        .panel:nth-child(3n) { border-right: none; }

        .panel-header {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            padding: 10px 22px 8px;
            border-bottom: 1px solid var(--line);
            flex-shrink: 0;
        }

        .panel-title {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.62rem;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 2.5px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .panel-title::before {
            content: '';
            display: inline-block;
            width: 8px; height: 8px;
            border: 1px solid var(--amber);
            transform: rotate(45deg);
            flex-shrink: 0;
        }

        .panel-tag {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.58rem;
            color: var(--text-3);
            border: 1px solid var(--line);
            padding: 2px 8px;
            border-radius: 2px;
        }

        /* ── LLM Panel (spans full width, row 2) ── */
        .panel-llm {
            grid-column: 1 / -1;
        }

        .llm-body {
            display: grid;
            grid-template-columns: 300px 280px 1fr;
            flex: 1;
            min-height: 0;
            overflow: hidden;
        }

        .llm-col {
            border-right: 1px solid var(--line);
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .llm-col:last-child { border-right: none; }

        .llm-col-label {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.58rem;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 2px;
            padding: 6px 16px;
            border-bottom: 1px solid var(--line-2);
            flex-shrink: 0;
        }

        .llm-text {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.72rem;
            line-height: 1.7;
            color: var(--text-2);
            padding: 12px 16px;
            overflow-y: auto;
            flex: 1;
            white-space: pre-wrap;
            word-break: break-word;
            max-height: 450px;
        }

        .llm-text.response {
            color: var(--amber);
            font-size: 0.73rem;
        }

        .llm-text.thought {
            color: var(--text-3);
            font-style: italic;
        }

        /* ── Data Tables ── */
        .panel-body {
            flex: 1;
            overflow-y: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        thead tr {
            border-bottom: 1px solid var(--line);
        }

        th {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.57rem;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 1.5px;
            padding: 10px 18px;
            text-align: left;
            font-weight: 400;
            white-space: nowrap;
        }

        td {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.72rem;
            color: var(--text-2);
            padding: 11px 18px;
            border-bottom: 1px solid var(--line-2);
        }

        tbody tr { transition: background 0.15s ease; cursor: pointer; }
        tbody tr:hover { background: rgba(255,255,255,0.018); }
        tbody tr:hover td { color: var(--text); }
        tbody tr:last-child td { border-bottom: none; }

        .tk {
            font-family: 'IBM Plex Mono', monospace;
            font-weight: 600;
            font-size: 0.72rem;
            color: var(--text);
            letter-spacing: 0.5px;
        }

        /* ── Badges ── */
        .badge {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.58rem;
            font-weight: 600;
            letter-spacing: 1px;
            padding: 3px 8px;
            border-radius: 2px;
            text-transform: uppercase;
            display: inline-block;
        }

        .badge.LONG, .badge.BUY, .badge.STRONG_BUY {
            color: var(--green);
            background: var(--green-dim);
            border: 1px solid rgba(82,168,130,0.25);
        }

        .badge.SHORT, .badge.SELL, .badge.AVOID {
            color: var(--red);
            background: var(--red-dim);
            border: 1px solid rgba(192,80,74,0.25);
        }

        .badge.WATCH, .badge.SKIP, .badge.HOLD {
            color: var(--yellow);
            background: var(--yellow-dim);
            border: 1px solid rgba(200,168,75,0.25);
        }

        .badge.UNKNOWN {
            color: var(--text-3);
            background: transparent;
            border: 1px solid var(--line);
        }

        /* ── Score Bar ── */
        .score-wrap {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .score-bar {
            width: 48px;
            height: 3px;
            background: var(--line);
        }

        .score-fill {
            height: 100%;
            background: var(--amber);
        }

        /* ── Modal ── */
        #modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(8,8,7,0.92);
            backdrop-filter: blur(6px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 999;
            padding: 40px;
        }

        .modal-container {
            background: var(--bg-2);
            border: 1px solid var(--line);
            width: 100%;
            max-width: 1240px;
            height: 88vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .modal-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 18px 28px;
            border-bottom: 1px solid var(--line);
            flex-shrink: 0;
        }

        #modal-title {
            font-family: 'Playfair Display', serif;
            font-size: 1.1rem;
            font-weight: 700;
            color: var(--text);
        }

        .modal-close {
            width: 28px; height: 28px;
            border: 1px solid var(--line);
            background: transparent;
            color: var(--text-3);
            font-size: 1rem;
            cursor: pointer;
            display: flex; align-items: center; justify-content: center;
            transition: all 0.15s;
        }

        .modal-close:hover { border-color: var(--amber); color: var(--amber); }

        .modal-body {
            display: grid;
            grid-template-columns: 1fr 320px;
            flex: 1;
            min-height: 0;
            overflow: hidden;
        }

        .modal-main {
            display: flex;
            flex-direction: column;
            overflow: hidden;
            border-right: 1px solid var(--line);
        }

        .modal-tabs {
            display: flex;
            border-bottom: 1px solid var(--line);
            flex-shrink: 0;
        }

        .modal-tab {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.62rem;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            padding: 12px 20px;
            border: none;
            border-right: 1px solid var(--line);
            border-bottom: 2px solid transparent;
            background: transparent;
            color: var(--text-3);
            cursor: pointer;
            transition: all 0.15s;
        }

        .modal-tab.active {
            color: var(--amber);
            border-bottom-color: var(--amber);
            background: var(--amber-dim);
        }

        .modal-tab:hover:not(.active) { color: var(--text-2); background: var(--line-2); }

        .tab-content {
            flex: 1;
            overflow-y: auto;
            padding: 28px;
        }

        .tab-content[style*="display: none"] { display: none !important; }

        .chart-container {
            width: 100%;
            height: 380px;
            background: var(--bg);
            border: 1px solid var(--line);
            margin-bottom: 24px;
            overflow: hidden;
        }

        #analysis-summary {
            font-size: 0.88rem;
            line-height: 1.8;
            color: var(--text-2);
        }

        #analysis-summary h3 {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.62rem;
            color: var(--amber);
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-bottom: 10px;
            margin-top: 20px;
        }

        #analysis-summary h3:first-child { margin-top: 0; }

        #analysis-summary p { color: var(--text-2); line-height: 1.8; }

        #analysis-summary ul {
            padding-left: 16px;
            color: var(--text-2);
        }

        #analysis-summary li { margin-bottom: 6px; }

        .mono-box {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.72rem;
            line-height: 1.7;
            color: var(--text-2);
            background: var(--bg);
            border: 1px solid var(--line);
            padding: 20px;
            white-space: pre-wrap;
            word-break: break-word;
            min-height: 300px;
            max-height: 600px;
            overflow-y: auto;
        }

        .mono-box.thought { color: var(--text-3); font-style: italic; }
        .mono-box.json-out { color: var(--green); }

        .modal-sidebar {
            padding: 24px;
            overflow-y: auto;
            background: var(--bg);
        }

        .sidebar-block {
            margin-bottom: 24px;
        }

        .sidebar-label {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.58rem;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-bottom: 8px;
        }

        .sidebar-value {
            font-family: 'Playfair Display', serif;
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--text);
        }

        /* ── Animations ── */
        @keyframes slideUp {
            from { opacity: 0; transform: translateY(8px); }
            to   { opacity: 1; transform: translateY(0); }
        }

        .slide-up { animation: slideUp 0.3s ease-out both; }

        /* ── Empty state ── */
        .empty-row td {
            color: var(--text-3);
            font-style: italic;
            text-align: center;
            padding: 32px;
        }
    </style>
</head>
<body>
<div class="shell">

    <!-- ══ TOP BAR ══ -->
    <header class="topbar">
        <div class="topbar-brand">
            <div class="brand-mark">G</div>
            <div>
                <div class="brand-name">GeoPoTech</div>
                <div class="brand-sub">Trading Intelligence</div>
            </div>
        </div>

        <div class="topbar-status">
            <div class="status-pip"></div>
            <span id="stage-text">Initializing…</span>
        </div>

        <div class="topbar-progress">
            <div class="prog-track">
                <div class="prog-fill" id="stage-progress-bar"></div>
            </div>
            <span id="stage-progress-text">0 / 0 items</span>
        </div>

        <div class="topbar-right">
            <span id="model-name"></span>
        </div>
    </header>

    <!-- ══ TASK BAR ══ -->
    <div id="task-monitor">
        <span id="current-task-name">—</span>
        <div class="task-track">
            <div id="task-progress-bar"></div>
        </div>
        <span id="eta-display">ETA: —</span>
    </div>

    <!-- ══ MAIN CONTENT ══ -->
    <div class="main">

        <!-- Stat Strip -->
        <div class="stat-strip">
            <div class="stat-cell">
                <div class="stat-label">Articles Fetched</div>
                <div class="stat-value" id="articles_fetched">0</div>
            </div>
            <div class="stat-cell">
                <div class="stat-label">Podcasts Transcribed</div>
                <div class="stat-value" id="podcasts_transcribed">0</div>
            </div>
            <div class="stat-cell">
                <div class="stat-label">Ideas Extracted</div>
                <div class="stat-value" id="ideas_extracted">0</div>
            </div>
            <div class="stat-cell">
                <div class="stat-label">Analyst Scored</div>
                <div class="stat-value" id="ideas_scored">0</div>
            </div>
        </div>

        <!-- LLM Monitor — full width row -->
        <div class="panel panel-llm" style="grid-column: 1/-1;">
            <div class="panel-header">
                <div class="panel-title">Live Intelligence Monitor</div>
                <div class="panel-tag">STREAMING</div>
            </div>
            <div class="llm-body" style="flex: 1;">
                <div class="llm-col">
                    <div class="llm-col-label">Current Prompt</div>
                    <div class="llm-text" id="llm-prompt">Idle…</div>
                </div>
                <div class="llm-col">
                    <div class="llm-col-label">AI Reasoning</div>
                    <div class="llm-text thought" id="llm-thought">Idle…</div>
                </div>
                <div class="llm-col">
                    <div class="llm-col-label">AI Output</div>
                    <div class="llm-text response" id="llm-response">Idle…</div>
                </div>
            </div>
        </div>

        <!-- Stage 1 -->
        <div class="panel">
            <div class="panel-header">
                <div class="panel-title">Stage 1 — Extraction</div>
                <div class="panel-tag">IDEAS</div>
            </div>
            <div class="panel-body">
                <table>
                    <thead>
                        <tr>
                            <th>Ticker</th>
                            <th>Bias</th>
                            <th>Headline</th>
                            <th>Conv.</th>
                        </tr>
                    </thead>
                    <tbody id="ideas_table">
                        <tr class="empty-row"><td colspan="4">No data yet</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Stage 2 -->
        <div class="panel">
            <div class="panel-header">
                <div class="panel-title">Stage 2 — Research Plans</div>
                <div class="panel-tag">PLANS</div>
            </div>
            <div class="panel-body">
                <table>
                    <thead>
                        <tr>
                            <th>Ticker</th>
                            <th>Logic Summary</th>
                            <th>Queries</th>
                        </tr>
                    </thead>
                    <tbody id="plans_table">
                        <tr class="empty-row"><td colspan="3">No data yet</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Stage 3 -->
        <div class="panel">
            <div class="panel-header">
                <div class="panel-title">Stage 3 — Analyst Scored</div>
                <div class="panel-tag">REPORTS</div>
            </div>
            <div class="panel-body">
                <table>
                    <thead>
                        <tr>
                            <th>Ticker</th>
                            <th>Rec.</th>
                            <th>Score</th>
                            <th>R/R</th>
                        </tr>
                    </thead>
                    <tbody id="reports_table">
                        <tr class="empty-row"><td colspan="4">No data yet</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

    </div><!-- /main -->
</div><!-- /shell -->

<!-- ══ MODAL ══ -->
<div id="modal-overlay">
    <div class="modal-container">
        <div class="modal-header">
            <div id="modal-title">Item Details</div>
            <button class="modal-close" onclick="closeModal()">✕</button>
        </div>
        <div class="modal-body">
            <div class="modal-main">
                <div class="modal-tabs">
                    <button class="modal-tab active" onclick="switchTab('analysis', this)">Analysis</button>
                    <button class="modal-tab" onclick="switchTab('prompt', this)">Input Prompt</button>
                    <button class="modal-tab" onclick="switchTab('thinking', this)">Thinking Trace</button>
                    <button class="modal-tab" onclick="switchTab('json', this)">Raw JSON</button>
                </div>

                <div id="tab-analysis" class="tab-content">
                    <div class="chart-container" id="chart-parent"></div>
                    <div id="analysis-summary"></div>
                </div>

                <div id="tab-prompt" class="tab-content" style="display:none;">
                    <div class="mono-box" id="modal-prompt"></div>
                </div>

                <div id="tab-thinking" class="tab-content" style="display:none;">
                    <div class="mono-box thought" id="modal-thinking"></div>
                </div>

                <div id="tab-json" class="tab-content" style="display:none;">
                    <div class="mono-box json-out" id="modal-json"></div>
                </div>
            </div>

            <div class="modal-sidebar" id="modal-sidebar-content"></div>
        </div>
    </div>
</div>

<!-- ══ HIDDEN elements that original JS references ══ -->
<tbody id="actions_table" style="display:none;"></tbody>

<script>
    let cachedState = {};

    function updateBadge(val) {
        return `<span class="badge ${val || 'UNKNOWN'}">${val || 'N/A'}</span>`;
    }

    function scoreBar(score, max = 10) {
        const pct = Math.min(100, (score / max) * 100);
        return `<div class="score-wrap">
            <span style="color:var(--text);font-weight:600;">${score}</span>
            <div class="score-bar"><div class="score-fill" style="width:${pct}%"></div></div>
        </div>`;
    }

    function switchTab(tab, btn) {
        document.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
        document.querySelectorAll('.modal-tab').forEach(el => el.classList.remove('active'));
        document.getElementById(`tab-${tab}`).style.display = 'block';
        if (btn) btn.classList.add('active');
    }

    function loadTickerChart(ticker) {
        const container = document.getElementById('chart-parent');
        container.innerHTML = '';
        if (!ticker || ticker === 'UNKNOWN' || ticker === 'N/A') {
            container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-3);font-family:IBM Plex Mono,monospace;font-size:0.72rem;">No ticker for chart</div>';
            return;
        }
        const script = document.createElement('script');
        script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
        script.type = 'text/javascript';
        script.async = true;
        script.innerHTML = JSON.stringify({
            "autosize": true,
            "symbol": ticker,
            "interval": "D",
            "timezone": "Etc/UTC",
            "theme": "dark",
            "style": "1",
            "locale": "en",
            "enable_publishing": false,
            "hide_top_toolbar": true,
            "backgroundColor": "rgba(8,8,7,1)",
            "gridColor": "rgba(255,255,255,0.04)",
            "allow_symbol_change": false,
            "save_image": false,
            "container_id": "chart-parent"
        });
        container.appendChild(script);
    }

    function showDetail(type, index) {
        const data = cachedState[type][index];
        const ticker = data.ticker || 'N/A';

        document.getElementById('modal-title').innerText = `${ticker} — ${type.replace(/_data$/, '').toUpperCase()}`;
        document.getElementById('modal-json').innerText = JSON.stringify(data, null, 2);
        document.getElementById('modal-thinking').innerText = data.thinking_trace || 'No reasoning recorded.';
        document.getElementById('modal-prompt').innerText = data.input_prompt || 'Prompt not archived.';

        let summary = '';
        if (type === 'reports_data') {
            summary = `<h3>Investment Thesis</h3><p>${data.thesis || 'N/A'}</p>`;
            summary += `<h3>Catalysts</h3><ul>${(data.catalysts || []).map(c => `<li>${c}</li>`).join('')}</ul>`;
            summary += `<h3>Risk Assessment</h3><p>${data.risk_assessment || 'N/A'}</p>`;
        } else if (type === 'ideas_data') {
            summary = `<h3>Core Idea</h3><p>${data.thesis_1sentence || data.headline || 'N/A'}</p>`;
        } else if (type === 'actions_data') {
            summary = `<h3>Portfolio Rationale</h3><p>${data.reasoning || 'N/A'}</p>`;
        }
        document.getElementById('analysis-summary').innerHTML = summary;

        // Sidebar
        let sidebar = '';
        if (data.recommendation || data.direction) {
            sidebar += `<div class="sidebar-block">
                <div class="sidebar-label">Sentiment</div>
                <div style="margin-top:4px;">${updateBadge(data.recommendation || data.direction)}</div>
            </div>`;
        }
        if (data.scores) {
            sidebar += `<div class="sidebar-block">
                <div class="sidebar-label">Overall Score</div>
                <div class="sidebar-value">${data.scores.overall}<span style="font-size:1rem;color:var(--text-3)">/10</span></div>
            </div>`;
        }
        if (data.risk_reward_ratio) {
            sidebar += `<div class="sidebar-block">
                <div class="sidebar-label">Risk / Reward</div>
                <div style="font-family:'IBM Plex Mono',monospace;font-size:0.9rem;color:var(--text);margin-top:4px;">${data.risk_reward_ratio}</div>
            </div>`;
        }
        document.getElementById('modal-sidebar-content').innerHTML = sidebar || '<div style="color:var(--text-3);font-family:IBM Plex Mono,monospace;font-size:0.7rem;">No metadata</div>';

        // Reset to analysis tab
        switchTab('analysis', document.querySelector('.modal-tab'));
        loadTickerChart(ticker);
        document.getElementById('modal-overlay').style.display = 'flex';
    }

    function closeModal() {
        document.getElementById('modal-overlay').style.display = 'none';
    }

    document.getElementById('modal-overlay').addEventListener('click', (e) => {
        if (e.target === document.getElementById('modal-overlay')) closeModal();
    });

    async function refresh() {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();
            cachedState = data;

            document.getElementById('stage-text').innerText = data.stage || '—';
            document.getElementById('articles_fetched').innerText = data.articles_fetched;
            document.getElementById('podcasts_transcribed').innerText = data.podcasts_transcribed;
            document.getElementById('ideas_extracted').innerText = data.ideas_extracted;
            document.getElementById('ideas_scored').innerText = data.ideas_scored;

            const total = data.total_items || 0;
            const current = data.current_item_index || 0;
            const pct = total > 0 ? (current / total) * 100 : 0;
            document.getElementById('stage-progress-bar').style.width = pct + '%';
            document.getElementById('stage-progress-text').innerText = `${current} / ${total} items`;

            document.getElementById('model-name').innerText = data.llm_model ? `[${data.llm_model}]` : '';
            document.getElementById('llm-prompt').innerText = data.llm_prompt || 'Idle…';
            document.getElementById('llm-thought').innerText = data.llm_thought || 'Idle…';
            document.getElementById('llm-response').innerText = data.llm_response || 'Idle…';

            // Task monitor
            const tm = document.getElementById('task-monitor');
            if (data.current_task) {
                tm.style.display = 'flex';
                document.getElementById('current-task-name').innerText = `${data.current_task}  (${data.task_progress || 0}%)`;
                document.getElementById('task-progress-bar').style.width = (data.task_progress || 0) + '%';
                if (data.eta_seconds > 0) {
                    const m = Math.floor(data.eta_seconds / 60);
                    const s = Math.floor(data.eta_seconds % 60);
                    document.getElementById('eta-display').innerText = `ETA ${m}m ${s}s`;
                } else {
                    document.getElementById('eta-display').innerText = 'ETA calculating…';
                }
            } else {
                tm.style.display = 'none';
            }

            // Ideas
            const ideasHtml = data.ideas_data.length
                ? data.ideas_data.map((i, idx) => `
                    <tr onclick="showDetail('ideas_data', ${idx})">
                        <td class="tk">${i.ticker || '—'}</td>
                        <td>${updateBadge(i.direction)}</td>
                        <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${i.headline || '—'}</td>
                        <td>${i.conviction_from_sources || 0}/10</td>
                    </tr>`).join('')
                : '<tr class="empty-row"><td colspan="4">No data yet</td></tr>';
            document.getElementById('ideas_table').innerHTML = ideasHtml;

            // Plans
            const plansHtml = data.plans_data.length
                ? data.plans_data.map((p, idx) => `
                    <tr onclick="showDetail('plans_data', ${idx})">
                        <td class="tk">${p.ticker || '—'}</td>
                        <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${p.thought || '—'}</td>
                        <td>${(p.queries || []).length}</td>
                    </tr>`).join('')
                : '<tr class="empty-row"><td colspan="3">No data yet</td></tr>';
            document.getElementById('plans_table').innerHTML = plansHtml;

            // Reports
            const reportsHtml = data.reports_data.length
                ? data.reports_data.map((r, idx) => `
                    <tr onclick="showDetail('reports_data', ${idx})">
                        <td class="tk">${r.ticker || '—'}</td>
                        <td>${updateBadge(r.recommendation)}</td>
                        <td>${scoreBar(r.scores ? r.scores.overall : 0)}</td>
                        <td>${r.risk_reward_ratio || '—'}</td>
                    </tr>`).join('')
                : '<tr class="empty-row"><td colspan="4">No data yet</td></tr>';
            document.getElementById('reports_table').innerHTML = reportsHtml;

            // Actions (hidden, kept for compatibility)
            document.getElementById('actions_table').innerHTML = data.actions_data.map((a, idx) => `
                <tr onclick="showDetail('actions_data', ${idx})">
                    <td>${a.ticker || '—'}</td>
                    <td>${updateBadge(a.action)}</td>
                    <td>${a.change_pct}%</td>
                    <td>${a.reasoning}</td>
                </tr>`).join('');

        } catch (e) {
            console.error('Dashboard poll error:', e);
        }
    }

    setInterval(refresh, 1000);
    refresh();
</script>
</body>
</html>"""


async def handle_index(request):
    return web.Response(text=HTML_TEMPLATE, content_type='text/html')

async def handle_api_status(request):
    import time
    eta = state.eta_seconds
    
    # Calculate sub-task ETA (e.g. current Whisper transcription)
    if state.current_task and state.task_progress > 0 and state.task_start_time > 0:
        elapsed = time.time() - state.task_start_time
        prog = max(1, state.task_progress)
        total_est = (elapsed / prog) * 100
        eta = total_est - elapsed
    elif eta == 0 and state.total_items > 0 and state.current_item_index > 0 and state.start_time > 0:
        elapsed = time.time() - state.start_time
        avg_time = elapsed / state.current_item_index
        remaining = state.total_items - state.current_item_index
        eta = avg_time * remaining

    data = {
        "stage": state.stage,
        "articles_fetched": state.articles_fetched,
        "articles_summarized": state.articles_summarized,
        "ideas_extracted": state.ideas_extracted,
        "ideas_scored": state.ideas_scored,
        "podcasts_transcribed": state.podcasts_transcribed,
        "transcription_progress": state.transcription_progress,
        "transcription_current_podcast": state.transcription_current_podcast,
        "current_item_index": state.current_item_index,
        "total_items": state.total_items,
        "current_task": state.current_task,
        "task_progress": state.task_progress,
        "eta_seconds": eta,
        "llm_model": state.llm_model,
        "llm_prompt": state.llm_prompt,
        "llm_thought": state.llm_thought,
        "llm_response": state.llm_response,
        "ideas_data": state.ideas_data,
        "plans_data": state.plans_data,
        "reports_data": state.reports_data,
        "actions_data": state.actions_data,
    }
    return web.json_response(data)

async def start_dashboard(port=8080):
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/api/status', handle_api_status)
    
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
    except OSError as e:
        if e.errno == 98:
            print(f"\n\033[91m[ERROR]\033[0m Port {port} is already in use. The dashboard might already be running.")
            print("\033[91m[ERROR]\033[0m Continuing without starting a new dashboard instance...\n")
            return runner
        raise

    sys.stdout.write(f"\n\033[94m[DASHBOARD]\033[0m Live monitor available at: http://0.0.0.0:{port}\n")
    sys.stdout.write(f"\033[94m[DASHBOARD]\033[0m If on a remote machine, use: http://<remote-ip>:{port}\n\n")
    sys.stdout.flush()

    try:
        is_ssh = any(k in os.environ for k in ["SSH_CLIENT", "SSH_TTY", "SSH_CONNECTION"])
        has_display = "DISPLAY" in os.environ
        if has_display and not is_ssh:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
    except Exception:
        pass
        
    return runner