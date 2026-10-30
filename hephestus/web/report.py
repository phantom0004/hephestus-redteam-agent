"""Render an engagement handoff snapshot into a professional, auditable PDF.

The PDF is built entirely from the in-memory handoff dictionary produced by
``AgentRunner.export_snapshot``. It is designed to be handed to a blue team or
an external reviewer: a generic security audit cover page, audit metadata, the original
prompt and scope, runtime configuration, the full command-output evidence, a
chronological activity timeline and a cryptographic integrity attestation.
"""

from datetime import datetime, timezone
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)

PAGE_WIDTH, PAGE_HEIGHT = A4
LEFT_MARGIN = RIGHT_MARGIN = 18 * mm
TOP_MARGIN = 24 * mm
BOTTOM_MARGIN = 20 * mm
CONTENT_WIDTH = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")
NAVY = colors.HexColor("#0b2f5c")
ACCENT = colors.HexColor("#1d4ed8")
RULE = colors.HexColor("#d4dae5")
PANEL = colors.HexColor("#f4f6fb")
OUTPUT_CLIP = 6000



_EVENT_TITLES = {
    "environment_info": "Runtime environment",
    "start_run": "Task started",
    "config_snapshot": "Configuration captured",
    "planner_request": "Model planning request",
    "planner_output": "Model plan",
    "proposed_commands": "Proposed command(s)",
    "policy_decision": "Policy decision",
    "policy_block": "Policy block",
    "safety_review": "Safety & scope review",
    "safety_block": "Safety/scope block",
    "edit_request": "Operator edit requested",
    "edited_command": "Command edited",
    "approval_decision": "Operator approval decision",
    "command_dispatch": "Command dispatched",
    "execution_result": "Command result",
    "observation": "Observation recorded",
    "premature_completion": "Premature completion rejected",
    "no_commands": "No command extracted",
    "duplicate_command": "Duplicate command skipped",
    "run_complete": "Run complete",
    "run_terminated": "Run terminated",
}


def _styles():
    base = getSampleStyleSheet()
    styles = {
        "h1": ParagraphStyle("h1", keepWithNext=True, parent=base["Heading1"], fontName="Helvetica-Bold",
                             fontSize=15, textColor=NAVY, spaceBefore=4, spaceAfter=8,
                             leading=18),
        "h2": ParagraphStyle("h2", keepWithNext=True, parent=base["Heading2"], fontName="Helvetica-Bold",
                             fontSize=11, textColor=ACCENT, spaceBefore=10, spaceAfter=5,
                             leading=14),
        "body": ParagraphStyle("body", parent=base["BodyText"], fontName="Helvetica",
                               fontSize=9.5, textColor=INK, leading=14, spaceAfter=4),
        "muted": ParagraphStyle("muted", parent=base["BodyText"], fontName="Helvetica",
                                fontSize=8.5, textColor=MUTED, leading=12),
        "cell": ParagraphStyle("cell", parent=base["BodyText"], fontName="Helvetica",
                               fontSize=8.5, textColor=INK, leading=12),
        "cellhead": ParagraphStyle("cellhead", parent=base["BodyText"],
                                   fontName="Helvetica-Bold", fontSize=8.5,
                                   textColor=colors.white, leading=12),
        "mono": ParagraphStyle("mono", parent=base["BodyText"], fontName="Courier",
                               fontSize=7.5, textColor=INK, leading=10,
                               wordWrap="CJK"),
        "monolight": ParagraphStyle("monolight", parent=base["BodyText"],
                                    fontName="Courier", fontSize=7.8,
                                    textColor=colors.HexColor("#e6edf7"), leading=11),
    }
    return styles


def _fmt_time(value):
    if not value:
        return "—"
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S %Z").strip() or parsed.isoformat()
    except ValueError:
        return text


def _esc(value):
    return escape("" if value is None else str(value))


def _draw_cover(canvas, doc, meta):
    canvas.saveState()
    title_y = PAGE_HEIGHT - 85 * mm
    # Title block.
    canvas.setFillColor(NAVY)
    canvas.setFont("Helvetica-Bold", 24)
    canvas.drawCentredString(PAGE_WIDTH / 2, title_y, "Security Audit")
    canvas.setFillColor(INK)
    canvas.setFont("Helvetica", 14)
    canvas.drawCentredString(PAGE_WIDTH / 2, title_y - 10 * mm,
                             "Cybersecurity Activity & Evidence Report")
    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(1.4)
    canvas.line(PAGE_WIDTH / 2 - 35 * mm, title_y - 15 * mm,
                PAGE_WIDTH / 2 + 35 * mm, title_y - 15 * mm)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 10)
    canvas.drawCentredString(PAGE_WIDTH / 2, title_y - 23 * mm,
                             _esc(meta.get("title") or "Untitled engagement"))

    # Audit summary strip near the bottom.
    rows = [
        ("Report generated", _fmt_time(meta.get("exported_at"))),
        ("Run identifier", meta.get("run_id") or "—"),
        ("Final status", str(meta.get("status") or "—").replace("_", " ")),
        ("Timeline SHA-256", meta.get("timeline_sha256") or "—"),
    ]
    y = 56 * mm
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.8)
    canvas.line(LEFT_MARGIN, y + 8 * mm, PAGE_WIDTH - RIGHT_MARGIN, y + 8 * mm)
    for label, value in rows:
        canvas.setFont("Helvetica-Bold", 8.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(LEFT_MARGIN, y, label.upper())
        canvas.setFont("Courier" if "SHA" in label or label == "Run identifier" else "Helvetica", 8.5)
        canvas.setFillColor(INK)
        canvas.drawString(LEFT_MARGIN + 42 * mm, y, _truncate(str(value), 78))
        y -= 6.2 * mm
    canvas.setFont("Helvetica-Oblique", 8)
    canvas.setFillColor(MUTED)
    canvas.drawCentredString(
        PAGE_WIDTH / 2, 22 * mm,
        "Authorized-engagement evidence. A completed run is not a verified result — review before relying on it.",
    )
    canvas.restoreState()


def _draw_body_chrome(canvas, doc, meta):
    canvas.saveState()
    # Header.
    canvas.setFont("Helvetica-Bold", 9)
    canvas.setFillColor(NAVY)
    canvas.drawString(LEFT_MARGIN, PAGE_HEIGHT - 14 * mm, "SECURITY AUDIT · Activity Evidence")
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(PAGE_WIDTH - RIGHT_MARGIN, PAGE_HEIGHT - 14 * mm,
                           _truncate(str(meta.get("title") or ""), 60))
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.8)
    canvas.line(LEFT_MARGIN, PAGE_HEIGHT - 16 * mm, PAGE_WIDTH - RIGHT_MARGIN, PAGE_HEIGHT - 16 * mm)
    # Footer.
    canvas.line(LEFT_MARGIN, 15 * mm, PAGE_WIDTH - RIGHT_MARGIN, 15 * mm)
    canvas.setFont("Courier", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawCentredString(PAGE_WIDTH / 2, 11 * mm, f"run {str(meta.get('run_id') or '')[:16]}")
    canvas.restoreState()


def _truncate(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _NumberedCanvas(pdfcanvas.Canvas):
    """Defers page rendering so each body page can show 'Page X of Y'."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_states = []

    def showPage(self):
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_states)
        for index, state in enumerate(self._saved_states, start=1):
            self.__dict__.update(state)
            if index > 1:
                self.setFont("Helvetica", 7.5)
                self.setFillColor(MUTED)
                self.drawRightString(PAGE_WIDTH - RIGHT_MARGIN, 11 * mm,
                                     f"Page {index - 1} of {total - 1}")
            super().showPage()
        super().save()


def _section(title, flowables, styles):
    block = [Paragraph(title, styles["h1"])]
    block.extend(flowables)
    return block


def _kv_table(rows, styles):
    data = [[Paragraph(_esc(label), styles["cell"]), Paragraph(body, styles["cell"])]
            for label, body in rows]
    table = Table(data, colWidths=[46 * mm, CONTENT_WIDTH - 46 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), PANEL),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("LINEAFTER", (0, 0), (0, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
    ]))
    return table


_REMOTE_STATUS = {
    "approved": "approved and run on the remote PC",
    "rejected": "REJECTED by the remote-PC operator (N) — not executed",
    "quit": "remote-PC operator QUIT the worker (Q) — not executed",
    "update": "remote-PC operator requested an edit (U) — superseded by the edited command",
    "error": "remote worker error — not executed",
}
_APPROVAL_LABELS = {
    "approved": "approved",
    "approved_on_domain_pc": "approved (remote PC)",
    "forwarded_to_domain_pc": "sent to remote PC",
    "rejected": "rejected (workspace)",
    "aborted": "aborted (workspace)",
    "rejected_on_domain_pc": "rejected (remote PC)",
    "worker_quit": "worker quit (remote PC)",
}


def _event_detail(event, data):
    if event == "execution_result":
        parts = [f"Command: {data.get('command', '')}", f"Exit code: {data.get('exit_code', 'unknown')}"]
        if data.get("remote_status"):
            parts.append("Remote status: " + _REMOTE_STATUS.get(data["remote_status"], str(data["remote_status"])))
        if data.get("execution_uncertain"):
            parts.append("Execution outcome unknown; inspect the target before retrying")
        elif data.get("executed") is False:
            parts.append("Executed: no")
        return "\n".join(parts)
    if event in {"run_complete", "run_terminated"}:
        from hephestus.web.runner import termination_label
        reason = data.get("reason") or data.get("status")
        label, detail = termination_label(reason)
        return f"{label or reason}: {detail}" if detail else str(label or reason or "Run ended.")
    if event == "proposed_commands":
        return "\n".join(f"$ {c}" for c in (data.get("commands") or [])) or "No command proposed."
    if event == "planner_output":
        return str(data.get("text") or "(no visible text)")
    if event == "policy_decision":
        return f"{'Allowed' if data.get('allowed') else 'Blocked'}: {data.get('command', '')}"
    if event == "safety_review":
        return f"{'Safe & in scope' if data.get('allowed') else 'BLOCKED (' + str(data.get('source', 'safety')) + ')'}: {data.get('command', '')}\n{data.get('reason', '')}"
    if event in {"edit_request", "edited_command"}:
        parts = []
        if data.get("instruction"):
            parts.append(f"Instruction: {data.get('instruction')}")
        if data.get("variant"):
            parts.append(f"Edited: {data.get('variant')}")
        if data.get("command"):
            parts.append(f"Command: {data.get('command')}")
        return "\n".join(parts)
    if event == "approval_decision":
        return (f"Approved: {len(data.get('approved') or [])} · "
                f"Rejected: {len(data.get('rejected') or [])}")
    if event == "start_run":
        return f"Goal: {data.get('goal', '')}\nMode: {data.get('mode', 'goal')}"
    if event in {"run_complete", "run_terminated"}:
        return str(data.get("reason") or data.get("status") or "Run ended.")
    if not data:
        return ""
    bits = []
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)):
            bits.append(f"{key}: {value}")
    return "\n".join(bits)


def build_engagement_pdf(snapshot):
    """Return the engagement handoff as PDF bytes."""
    styles = _styles()
    session = snapshot.get("session") or {}
    integrity = snapshot.get("integrity") or {}
    configuration = snapshot.get("configuration") or {}
    commands = snapshot.get("commands") or []
    timeline = snapshot.get("timeline") or []

    meta = {
        "title": session.get("title"),
        "run_id": session.get("run_id"),
        "status": session.get("status"),
        "exported_at": snapshot.get("exported_at"),
        "timeline_sha256": integrity.get("timeline_sha256"),
    }

    buffer = BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=A4, title="Cybersecurity Audit Evidence Report",
        author="Generated with Hephestus", leftMargin=LEFT_MARGIN, rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
    )
    cover_frame = Frame(0, 0, PAGE_WIDTH, PAGE_HEIGHT, id="cover")
    body_frame = Frame(LEFT_MARGIN, BOTTOM_MARGIN, CONTENT_WIDTH,
                       PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN, id="body", leftPadding=0, rightPadding=0)
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[cover_frame],
                     onPage=lambda c, d: _draw_cover(c, d, meta)),
        PageTemplate(id="body", frames=[body_frame],
                     onPage=lambda c, d: _draw_body_chrome(c, d, meta)),
    ])

    story = [NextPageTemplate("body"), PageBreak()]

    # 1. Engagement scope & objective.
    scope_rows = [
        ("Objective (prompt)", _esc(session.get("goal")) or "—"),
        ("Operator context / scope", _esc(session.get("context")) or "(none provided)"),
        ("Workflow mode", _esc(session.get("mode") or "goal")),
        ("Final status", _esc(str(session.get("status") or "—").replace("_", " "))),
        ("How the run ended", (f"<b>{_esc(session.get('termination_label'))}</b><br/>{_esc(session.get('termination_detail'))}"
                               if session.get("termination_label") else "—")),
        ("Termination code", f'<font face="Courier">{_esc(session.get("termination_reason") or "—")}</font>'),
    ]
    story += _section("1 · Engagement scope & objective", [_kv_table(scope_rows, styles)], styles)

    decisions = snapshot.get("operator_decisions") or {}
    if decisions:
        decision_rows = [
            ("Approved in workspace", _esc(decisions.get("approved_in_workspace", 0))),
            ("Rejected in workspace", _esc(decisions.get("rejected_in_workspace", 0))),
            ("Aborted in workspace", _esc(decisions.get("aborted_in_workspace", 0))),
            ("Guidance updates", _esc(decisions.get("guidance_updates", 0))),
            ("Approved on remote PC (Y)", _esc(decisions.get("approved_on_domain_pc", 0))),
            ("Rejected on remote PC (N)", _esc(decisions.get("rejected_on_domain_pc", 0))),
            ("Worker quit on remote PC (Q)", _esc(decisions.get("worker_quit_on_domain_pc", 0))),
        ]
        story += [Spacer(1, 10)]
        story += _section("Operator decisions", [
            _kv_table(decision_rows, styles), Spacer(1, 3),
            Paragraph("Rejecting a command (N) and quitting the remote worker (Q) are recorded separately; "
                      "neither executes the command.", styles["muted"])], styles)

    # 2. Session & audit metadata.
    audit_rows = [
        ("Evidence collection tool", "Hephestus — supervised local command orchestration"),
        ("Session title", _esc(session.get("title")) or "—"),
        ("Run identifier", f'<font face="Courier">{_esc(session.get("run_id"))}</font>'),
        ("Created", _esc(_fmt_time(session.get("created_at")))),
        ("First event", _esc(_fmt_time(session.get("started_at")))),
        ("Last event", _esc(_fmt_time(session.get("ended_at")))),
        ("Report generated", _esc(_fmt_time(snapshot.get("exported_at")))),
        ("Schema", _esc(snapshot.get("schema"))),
        ("Model file", _esc(configuration.get("model_file")) or "—"),
        ("Execution backend", _esc(configuration.get("backend")) or "—"),
    ]
    story += [Spacer(1, 10)]
    story += _section("2 · Session &amp; audit metadata", [_kv_table(audit_rows, styles)], styles)

    # 3. Runtime configuration.
    runtime = configuration.get("runtime_settings") or {}
    cfg_rows = [(key.replace("_", " "), _esc(value))
                for key, value in runtime.items() if not isinstance(value, (list, dict))]
    if cfg_rows:
        story += [Spacer(1, 10)]
        story += _section("3 · Runtime configuration", [_kv_table(cfg_rows, styles)], styles)

    # 4. Command evidence summary.
    story += [Spacer(1, 10), Paragraph("4 · Command evidence", styles["h1"])]
    if commands:
        header = [Paragraph(h, styles["cellhead"]) for h in
                  ("#", "Command", "Policy", "Approval", "Exit", "Executed")]
        rows = [header]
        for index, item in enumerate(commands, start=1):
            policy = item.get("policy") or {}
            execution = item.get("execution") or {}
            policy_text = "—" if not policy else ("allow" if policy.get("allowed") else "block")
            rows.append([
                Paragraph(str(index), styles["cell"]),
                Paragraph(_esc(item.get("command")), styles["mono"]),
                Paragraph(policy_text, styles["cell"]),
                Paragraph(_esc(_APPROVAL_LABELS.get(item.get("approval"), item.get("approval") or "—")), styles["cell"]),
                Paragraph(_esc(execution.get("exit_code", "—")), styles["cell"]),
                Paragraph("unknown" if execution.get("execution_uncertain") else "yes" if execution.get("executed") else "no", styles["cell"]),
            ])
        table = Table(rows, colWidths=[8 * mm, CONTENT_WIDTH - 66 * mm, 16 * mm, 20 * mm, 12 * mm, 14 * mm],
                      repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PANEL]),
            ("GRID", (0, 0), (-1, -1), 0.4, RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story += [table, Spacer(1, 4),
                  Paragraph("Command and output SHA-256 digests are listed in §7 (integrity).",
                            styles["muted"])]
    else:
        story += [Paragraph("No command was proposed or executed in this session.", styles["body"])]

    # 5. Command output log (the proof).
    story += [Spacer(1, 10), Paragraph("5 · Command output log", styles["h1"]),
              Paragraph("Verbatim output captured from executed commands, in order.", styles["muted"]),
              Spacer(1, 4)]
    outputs = [e for e in timeline if e.get("event") == "execution_result"]
    if outputs:
        for index, entry in enumerate(outputs, start=1):
            data = entry.get("data") or {}
            event_hash = (entry.get("integrity") or {}).get("event_sha256", "")
            lines = [f"$ {data.get('command', '')}",
                     f"exit code: {data.get('exit_code', 'unknown')} · {_fmt_time(entry.get('ts'))}"]
            if data.get("remote_status"):
                lines.append("remote status: " + _REMOTE_STATUS.get(data["remote_status"], str(data["remote_status"])))
            stdout = str(data.get("stdout") or "")
            stderr = str(data.get("stderr") or "")
            if stdout:
                lines.append("\n[stdout]\n" + _clip(stdout))
            if stderr:
                lines.append("\n[stderr]\n" + _clip(stderr))
            if not stdout and not stderr:
                lines.append("\n(no output captured)")
            text = "\n".join(lines)
            heading = Paragraph(
                f"Evidence {index} — SHA-256 <font face='Courier'>{_esc(event_hash[:32])}…</font>",
                styles["h2"])
            box = _output_box(text, styles)
            # Keep short evidence blocks whole; let long ones flow across pages.
            story += [KeepTogether([heading, box]) if len(text) < 2200 else heading, ]
            if len(text) >= 2200:
                story += [box]
            story += [Spacer(1, 6)]
    else:
        story += [Paragraph("No command produced output in this session.", styles["body"])]

    # 6. Activity timeline.
    story += [Spacer(1, 6), Paragraph("6 · Full activity timeline", styles["h1"])]
    header = [Paragraph(h, styles["cellhead"]) for h in ("Time", "Event", "Detail")]
    rows = [header]
    for entry in timeline:
        data = entry.get("data") or {}
        event = entry.get("event", "")
        detail = _clip(_event_detail(event, data), 900)
        rows.append([
            Paragraph(_esc(_fmt_time(entry.get("ts"))), styles["cell"]),
            Paragraph(_esc(_EVENT_TITLES.get(event, event.replace("_", " "))), styles["cell"]),
            Paragraph(_esc(detail).replace("\n", "<br/>"), styles["cell"]),
        ])
    if len(rows) > 1:
        table = Table(rows, colWidths=[30 * mm, 34 * mm, CONTENT_WIDTH - 64 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PANEL]),
            ("GRID", (0, 0), (-1, -1), 0.4, RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story += [table]
    else:
        story += [Paragraph("No activity was recorded.", styles["body"])]

    # 7. Integrity & attestation.
    story += [Spacer(1, 10), Paragraph("7 · Integrity &amp; attestation", styles["h1"])]
    integrity_rows = [
        ("Algorithm", _esc(integrity.get("algorithm") or "sha256")),
        ("Timeline SHA-256", f'<font face="Courier">{_esc(integrity.get("timeline_sha256"))}</font>'),
        ("Final event SHA-256", f'<font face="Courier">{_esc(integrity.get("final_event_sha256"))}</font>'),
        ("Event count", _esc(len(timeline))),
    ]
    story += [_kv_table(integrity_rows, styles), Spacer(1, 6),
              Paragraph(_esc(integrity.get("scope") or
                             "Hashes verify this exported in-memory timeline after generation."),
                        styles["muted"])]

    doc.build(story, canvasmaker=_NumberedCanvas)
    return buffer.getvalue()


def _clip(text, limit=OUTPUT_CLIP):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} more characters omitted — see JSON export] …"


def _output_box(text, styles):
    inner = Preformatted(text, styles["monolight"], maxLineLength=98)
    table = Table([[inner]], colWidths=[CONTENT_WIDTH], splitInRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#0b1220")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#e6edf7")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#243049")),
    ]))
    return table
