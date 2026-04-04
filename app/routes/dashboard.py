import math
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.cache import get_cache
from app.services.poller import trigger_qa_all, trigger_qa_campaign, trigger_qa_workspace

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# ---------------------------------------------------------------------------
# Utility functions (used by routes and templates)
# ---------------------------------------------------------------------------

LEAD_STATUS_LABELS = {1: "Active", 2: "Paused", 3: "Completed", -1: "Bounced"}


def health_class(broken: int, total: int) -> str:
    """Return CSS modifier class for traffic light dot. Per D-08/D-09."""
    if total == 0:
        return "gray"
    pct = broken / total
    if pct < 0.02:
        return "green"
    elif pct <= 0.10:
        return "yellow"
    return "red"


def health_pct(broken: int, total: int) -> str:
    """Return percentage string like '3.2%' or '0%'. Guards zero division."""
    if total == 0:
        return "0%"
    return f"{broken / total * 100:.1f}%"


def freshness_class(ts: datetime | None) -> str:
    """Return CSS class for freshness indicator. Per D-18."""
    if ts is None:
        return "gray"
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 300:
        return "green"
    elif age < 900:
        return "amber"
    return "gray"


def freshness_text(ts: datetime | None) -> str:
    """Return human-readable freshness string."""
    if ts is None:
        return "Never scanned"
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 60:
        return "Just now"
    elif age < 3600:
        mins = int(age // 60)
        return f"{mins} min ago"
    hours = int(age // 3600)
    return f"{hours}h ago"


def total_leads_for_workspace(ws) -> int:
    """Sum total_leads across all campaigns in a workspace."""
    return sum(c.total_leads for c in ws.campaigns)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Overview page: all workspaces with health status. Per VIEW-01, D-04/D-05/D-06."""
    data = await get_cache().get_all()
    ws_display = []
    for ws in data.workspaces:
        ws_total = total_leads_for_workspace(ws)
        ws_display.append({
            "name": ws.workspace_name,
            "broken": ws.total_broken,
            "total": ws_total,
            "health": health_class(ws.total_broken, ws_total),
            "pct": health_pct(ws.total_broken, ws_total),
            "campaign_count": len(ws.campaigns),
            "freshness_cls": freshness_class(ws.last_checked),
            "freshness_txt": freshness_text(ws.last_checked),
            "error": ws.error,
        })
    return templates.TemplateResponse(request, "dashboard.html", {
        "workspaces": ws_display,
        "ws_count": len(ws_display),
    })


@router.get("/ws/{ws_name}", response_class=HTMLResponse)
async def workspace_detail(request: Request, ws_name: str):
    """Workspace detail page: campaign table. Per VIEW-02, VIEW-05, VIEW-06, D-03."""
    result = await get_cache().get_workspace(ws_name)
    if result is None:
        # Workspace not yet scanned — render empty state with scan button
        return templates.TemplateResponse(request, "workspace.html", {
            "ws_name": ws_name,
            "campaigns": [],
            "ws_broken": 0,
            "ws_total": 0,
            "ws_health": "gray",
            "ws_pct": "0%",
            "not_scanned": True,
        })
    ws_total = total_leads_for_workspace(result)
    campaigns_display = []
    for c in result.campaigns:
        affected_vars = list(c.issues_by_variable.keys())
        if len(affected_vars) > 2:
            var_text = ", ".join(affected_vars[:2]) + f" +{len(affected_vars) - 2} more"
        elif affected_vars:
            var_text = ", ".join(affected_vars)
        else:
            var_text = ""
        campaigns_display.append({
            "id": c.campaign_id,
            "name": c.campaign_name,
            "status": "active" if c.total_leads > 0 else "draft",
            "broken": c.broken_count,
            "total": c.total_leads,
            "health": health_class(c.broken_count, c.total_leads),
            "pct": health_pct(c.broken_count, c.total_leads),
            "var_text": var_text,
            "freshness_cls": freshness_class(c.last_checked),
            "freshness_txt": freshness_text(c.last_checked),
        })
    return templates.TemplateResponse(request, "workspace.html", {
        "ws_name": ws_name,
        "campaigns": campaigns_display,
        "ws_broken": result.total_broken,
        "ws_total": ws_total,
        "ws_health": health_class(result.total_broken, ws_total),
        "ws_pct": health_pct(result.total_broken, ws_total),
        "not_scanned": False,
    })


@router.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Scan API routes (HTMX endpoints, return partial HTML)
# ---------------------------------------------------------------------------


@router.post("/api/scan/all", response_class=HTMLResponse)
async def scan_all(request: Request):
    """Trigger QA across all workspaces and return refreshed workspace grid. Per OPS-01, D-16."""
    await trigger_qa_all()
    # Return current cached state — scan runs async in background
    data = await get_cache().get_all()
    ws_display = []
    for ws in data.workspaces:
        ws_total = total_leads_for_workspace(ws)
        ws_display.append({
            "name": ws.workspace_name,
            "broken": ws.total_broken,
            "total": ws_total,
            "health": health_class(ws.total_broken, ws_total),
            "pct": health_pct(ws.total_broken, ws_total),
            "campaign_count": len(ws.campaigns),
            "freshness_cls": freshness_class(ws.last_checked),
            "freshness_txt": freshness_text(ws.last_checked),
            "error": ws.error,
        })
    return templates.TemplateResponse(request, "_workspace_grid.html", {
        "workspaces": ws_display,
        "ws_count": len(ws_display),
    })


# ---------------------------------------------------------------------------
# Campaign detail page and scan endpoint
# ---------------------------------------------------------------------------

PAGE_SIZE = 25


@router.get("/ws/{ws_name}/campaign/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(request: Request, ws_name: str, campaign_id: str, page: int = 1):
    """Campaign detail page: variable breakdown + broken leads table. Per VIEW-03, VIEW-04, D-11/D-12/D-13."""
    result = await get_cache().get_workspace(ws_name)
    campaign = None
    if result:
        campaign = next((c for c in result.campaigns if c.campaign_id == campaign_id), None)

    if campaign is None:
        # Campaign not found in QA results — show not-scanned state
        return templates.TemplateResponse(request, "campaign.html", {
            "ws_name": ws_name,
            "campaign_id": campaign_id,
            "campaign_name": campaign_id,
            "not_scanned": True,
            "campaign_status": "draft",
            "total_leads": 0,
            "broken_count": 0,
            "health": "gray",
            "pct": "0%",
            "freshness_cls": "gray",
            "freshness_txt": "Never scanned",
            "variables": [],
            "page_leads": [],
            "page": 1,
            "total_pages": 1,
            "total_broken_count": 0,
            "lead_status_labels": LEAD_STATUS_LABELS,
        })

    # Variable summary sorted by count descending (per D-12, UI-SPEC)
    variables = sorted(
        [
            {"name": var_name, "count": count, "pct": count / campaign.total_leads * 100 if campaign.total_leads > 0 else 0}
            for var_name, count in campaign.issues_by_variable.items()
        ],
        key=lambda v: v["count"],
        reverse=True,
    )

    # Pagination (per D-15: 25 per page)
    total_broken_count = len(campaign.broken_leads)
    total_pages = max(1, math.ceil(total_broken_count / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    page_leads = campaign.broken_leads[start:start + PAGE_SIZE]

    # Format broken_vars for display: value -> display string
    def format_var_value(value):
        if value is None:
            return "[missing]"
        elif value == "":
            return "[empty]"
        elif value == "NO":
            return "NO"
        return value

    display_leads = []
    for bl in page_leads:
        formatted_vars = {k: format_var_value(v) for k, v in bl.broken_vars.items()}
        display_leads.append({
            "email": bl.email,
            "lead_status": LEAD_STATUS_LABELS.get(bl.lead_status, f"Unknown ({bl.lead_status})"),
            "broken_vars": formatted_vars,
        })

    return templates.TemplateResponse(request, "campaign.html", {
        "ws_name": ws_name,
        "campaign_id": campaign_id,
        "campaign_name": campaign.campaign_name,
        "not_scanned": False,
        "campaign_status": "active" if campaign.total_leads > 0 else "draft",
        "total_leads": campaign.total_leads,
        "broken_count": campaign.broken_count,
        "health": health_class(campaign.broken_count, campaign.total_leads),
        "pct": health_pct(campaign.broken_count, campaign.total_leads),
        "freshness_cls": freshness_class(campaign.last_checked),
        "freshness_txt": freshness_text(campaign.last_checked),
        "variables": variables,
        "page_leads": display_leads,
        "page": page,
        "total_pages": total_pages,
        "total_broken_count": total_broken_count,
        "lead_status_labels": LEAD_STATUS_LABELS,
    })


@router.post("/api/scan/ws/{ws_name}/campaign/{campaign_id}", response_class=HTMLResponse)
async def scan_campaign(request: Request, ws_name: str, campaign_id: str):
    """Trigger QA for one campaign and return refreshed results. Per OPS-03, D-16."""
    # Look up campaign dict from cache for trigger function
    result = await get_cache().get_workspace(ws_name)
    campaign_dict = None
    if result:
        for c in result.campaigns:
            if c.campaign_id == campaign_id:
                campaign_dict = {"id": campaign_id, "name": c.campaign_name}
                break

    if campaign_dict is None:
        # Try discovery cache
        discovered = await get_cache().get_campaigns(ws_name)
        campaign_dict = next((c for c in discovered if c.get("id") == campaign_id), None)

    if campaign_dict:
        await trigger_qa_campaign(campaign_id, campaign_dict, ws_name)

    # Re-render the campaign detail (fire-and-forget — show current cache state)
    return await campaign_detail(request, ws_name, campaign_id)


@router.post("/api/scan/ws/{ws_name}", response_class=HTMLResponse)
async def scan_workspace(request: Request, ws_name: str):
    """Trigger QA for one workspace and return refreshed campaign table. Per OPS-02, D-16."""
    await trigger_qa_workspace(ws_name)
    result = await get_cache().get_workspace(ws_name)
    campaigns_display = []
    if result:
        for c in result.campaigns:
            affected_vars = list(c.issues_by_variable.keys())
            if len(affected_vars) > 2:
                var_text = ", ".join(affected_vars[:2]) + f" +{len(affected_vars) - 2} more"
            elif affected_vars:
                var_text = ", ".join(affected_vars)
            else:
                var_text = ""
            campaigns_display.append({
                "id": c.campaign_id,
                "name": c.campaign_name,
                "status": "active" if c.total_leads > 0 else "draft",
                "broken": c.broken_count,
                "total": c.total_leads,
                "health": health_class(c.broken_count, c.total_leads),
                "pct": health_pct(c.broken_count, c.total_leads),
                "var_text": var_text,
                "freshness_cls": freshness_class(c.last_checked),
                "freshness_txt": freshness_text(c.last_checked),
            })
    return templates.TemplateResponse(request, "_campaign_table.html", {
        "ws_name": ws_name,
        "campaigns": campaigns_display,
        "not_scanned": False,
    })
