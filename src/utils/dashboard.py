import os
import json
from aiohttp import web

class DashboardState:
    def __init__(self):
        self.stage = "Initializing"
        self.articles_fetched = 0
        self.articles_summarized = 0
        self.ideas_extracted = 0
        self.ideas_scored = 0
        
        self.transcription_progress = 0  # 0-100
        self.transcription_current_podcast = ""
        
        self.ideas_data = []      # list of dicts
        self.reports_data = []    # list of dicts
        self.actions_data = []    # list of dicts

state = DashboardState()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Trading Agent Live Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #38bdf8;
            --border: rgba(71, 85, 105, 0.4);
            --success: #4ade80;
            --danger: #f87171;
            --warning: #facc15;
        }
        
        body { 
            font-family: 'Outfit', sans-serif; 
            background: radial-gradient(circle at top right, #1e1b4b, #0f172a);
            color: var(--text-main); 
            margin: 0; 
            padding: 40px; 
            min-height: 100vh;
        }
        
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
        }
        
        h1 { margin: 0; font-size: 2.5rem; font-weight: 700; background: linear-gradient(to right, #38bdf8, #818cf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        
        .status-badge {
            background: rgba(56, 189, 248, 0.1);
            border: 1px solid var(--accent);
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: 600;
            color: var(--accent);
            box-shadow: 0 0 15px rgba(56, 189, 248, 0.3);
            transition: all 0.3s ease;
        }

        .card { 
            background: var(--card-bg); 
            backdrop-filter: blur(12px);
            border: 1px solid var(--border);
            border-radius: 16px; 
            padding: 24px; 
            margin-bottom: 24px; 
            box-shadow: 0 10px 30px rgba(0,0,0,0.5); 
        }
        
        h2 { margin-top: 0; color: #e2e8f0; font-weight: 600; font-size: 1.5rem; display: flex; align-items: center; gap: 10px; }
        
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 10px; }
        
        .stat { 
            background: rgba(15, 23, 42, 0.6); 
            padding: 20px; 
            border-radius: 12px; 
            text-align: center; 
            border: 1px solid var(--border);
            transition: transform 0.2s;
        }
        .stat:hover { transform: translateY(-5px); border-color: var(--accent); }
        .stat-label { font-size: 0.9rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
        .stat-value { font-size: 2.5rem; font-weight: 700; color: var(--text-main); }
        
        /* Transcription Progress Bar */
        .progress-container {
            margin-top: 15px;
            background: rgba(15, 23, 42, 0.8);
            border-radius: 10px;
            height: 12px;
            overflow: hidden;
            border: 1px solid var(--border);
            display: none; /* Hidden by default */
        }
        .progress-bar {
            height: 100%;
            background: linear-gradient(to right, #38bdf8, #818cf8);
            width: 0%;
            transition: width 0.3s ease;
            box-shadow: 0 0 10px rgba(56, 189, 248, 0.5);
        }
        .progress-text {
            font-size: 0.85rem;
            color: var(--text-muted);
            margin-top: 8px;
            text-align: right;
            display: none;
        }

        .table-container { overflow-x: auto; }
        table { width: 100%; border-collapse: separate; border-spacing: 0; margin-top: 10px; }
        th, td { padding: 16px; text-align: left; border-bottom: 1px solid var(--border); }
        th { background: rgba(15, 23, 42, 0.8); color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 0.85rem; letter-spacing: 1px; }
        th:first-child { border-top-left-radius: 8px; }
        th:last-child { border-top-right-radius: 8px; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255, 255, 255, 0.03); }
        
        .badge { padding: 6px 12px; border-radius: 20px; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.5px; }
        .badge.LONG, .badge.BUY, .badge.STRONG_BUY { background: rgba(74, 222, 128, 0.15); color: var(--success); border: 1px solid rgba(74, 222, 128, 0.3); }
        .badge.SHORT, .badge.SELL, .badge.AVOID { background: rgba(248, 113, 113, 0.15); color: var(--danger); border: 1px solid rgba(248, 113, 113, 0.3); }
        .badge.WATCH, .badge.SKIP, .badge.HOLD { background: rgba(250, 204, 21, 0.15); color: var(--warning); border: 1px solid rgba(250, 204, 21, 0.3); }
        
        .ticker { font-family: monospace; font-size: 1.1rem; color: #fff; background: rgba(255,255,255,0.1); padding: 4px 8px; border-radius: 4px; }
        .fade-in { animation: fadeIn 0.5s ease-in-out; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body>
    <div class="header">
        <h1>Trading Agent</h1>
        <div class="status-badge" id="stage">Initializing Pipeline...</div>
    </div>
    
    <div class="card fade-in">
        <h2>Live Progress</h2>
        <div class="grid">
            <div class="stat">
                <div class="stat-label">Articles Fetched</div>
                <div class="stat-value" id="articles_fetched">0</div>
            </div>
            <div class="stat">
                <div class="stat-label">Batches Processed</div>
                <div class="stat-value" id="articles_summarized">0</div>
            </div>
            <div class="stat">
                <div class="stat-label">Ideas Extracted</div>
                <div class="stat-value" id="ideas_extracted">0</div>
            </div>
            <div class="stat">
                <div class="stat-label">Ideas Scored</div>
                <div class="stat-value" id="ideas_scored">0</div>
            </div>
        </div>

        <!-- Podcast Transcription Progress -->
        <div id="transcription-container">
            <div class="progress-text" id="transcription-label">Transcribing podcast...</div>
            <div class="progress-container" id="progress-parent">
                <div class="progress-bar" id="transcription-bar"></div>
            </div>
        </div>
    </div>

    <div class="card fade-in" id="ideas-section">
        <h2>Extracted Ideas (Stage 1)</h2>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>Ticker</th>
                        <th>Direction</th>
                        <th>Horizon</th>
                        <th>Headline</th>
                        <th>Media Score</th>
                    </tr>
                </thead>
                <tbody id="ideas_table"></tbody>
            </table>
        </div>
    </div>

    <div class="card fade-in" id="reports-section">
        <h2>Deep Research Reports (Stage 2)</h2>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>Ticker</th>
                        <th>Recommendation</th>
                        <th>Overall Score</th>
                        <th>Target Sizing</th>
                    </tr>
                </thead>
                <tbody id="reports_table"></tbody>
            </table>
        </div>
    </div>

    <div class="card fade-in" id="actions-section">
        <h2>Portfolio Actions (Stage 3)</h2>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>Ticker</th>
                        <th>Action</th>
                        <th>Allocation Change</th>
                        <th>Reasoning</th>
                    </tr>
                </thead>
                <tbody id="actions_table"></tbody>
            </table>
        </div>
    </div>

    <script>
        function updateBadge(val) {
            return `<span class="badge ${val || 'UNKNOWN'}">${val || 'N/A'}</span>`;
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                document.getElementById('stage').innerText = data.stage;
                document.getElementById('articles_fetched').innerText = data.articles_fetched;
                document.getElementById('articles_summarized').innerText = data.articles_summarized;
                document.getElementById('ideas_extracted').innerText = data.ideas_extracted;
                document.getElementById('ideas_scored').innerText = data.ideas_scored;

                // Update transcription progress
                const prog = data.transcription_progress || 0;
                const pod = data.transcription_current_podcast;
                const container = document.getElementById('progress-parent');
                const label = document.getElementById('transcription-label');
                const bar = document.getElementById('transcription-bar');

                if (pod) {
                    container.style.display = 'block';
                    label.style.display = 'block';
                    label.innerText = `Transcribing: ${pod} (${prog}%)`;
                    bar.style.width = prog + '%';
                } else {
                    container.style.display = 'none';
                    label.style.display = 'none';
                }

                const ideasHtml = data.ideas_data.map(i => `
                    <tr>
                        <td><span class="ticker">${i.ticker || 'N/A'}</span></td>
                        <td>${updateBadge(i.direction)}</td>
                        <td>${i.time_horizon || 'N/A'}</td>
                        <td style="max-width: 400px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${i.headline || 'N/A'}</td>
                        <td><strong>${i.conviction_from_sources || 0}/10</strong></td>
                    </tr>
                `).join('');
                document.getElementById('ideas_table').innerHTML = ideasHtml;

                const reportsHtml = data.reports_data.map(r => `
                    <tr>
                        <td><span class="ticker">${r.ticker || 'N/A'}</span></td>
                        <td>${updateBadge(r.recommendation)}</td>
                        <td><strong>${r.scores ? r.scores.overall : 0}/10</strong></td>
                        <td>${r.suggested_position_size_pct}%</td>
                    </tr>
                `).join('');
                document.getElementById('reports_table').innerHTML = reportsHtml;

                const actionsHtml = data.actions_data.map(a => `
                    <tr>
                        <td><span class="ticker">${a.ticker || 'N/A'}</span></td>
                        <td>${updateBadge(a.action)}</td>
                        <td><strong>${a.change_pct}%</strong></td>
                        <td style="max-width: 400px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${a.reasoning}</td>
                    </tr>
                `).join('');
                document.getElementById('actions_table').innerHTML = actionsHtml;

            } catch (e) {
                console.error("Dashboard disconnected", e);
            }
        }

        setInterval(fetchStatus, 2000);
        fetchStatus();
    </script>
</body>
</html>
"""

async def handle_index(request):
    return web.Response(text=HTML_TEMPLATE, content_type='text/html')

async def handle_api_status(request):
    data = {
        "stage": state.stage,
        "articles_fetched": state.articles_fetched,
        "articles_summarized": state.articles_summarized,
        "ideas_extracted": state.ideas_extracted,
        "ideas_scored": state.ideas_scored,
        "transcription_progress": state.transcription_progress,
        "transcription_current_podcast": state.transcription_current_podcast,
        "ideas_data": state.ideas_data,
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
    site = web.TCPSite(runner, 'localhost', port)
    await site.start()
    
    import webbrowser
    webbrowser.open(f"http://localhost:{port}")
    return runner
