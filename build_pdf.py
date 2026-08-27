import os
import json
import time
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, HRFlowable

from test_suite_50 import CASES, run_case

BASE_URL = "http://127.0.0.1:8005"
WORKSPACE_PDF = r"c:\Users\vansh\OneDrive\Desktop\controlpanel\controlplane\ControlPlane_50_Case_Benchmark_Report.pdf"
ARTIFACT_PDF = r"C:\Users\vansh\.gemini\antigravity\brain\cba8b450-635d-4b33-ab8f-a64a2ff0fe0c\ControlPlane_50_Case_Benchmark_Report.pdf"

def load_or_fetch_results():
    if os.path.exists("benchmark_results.json"):
        try:
            with open("benchmark_results.json", "r") as f:
                data = json.load(f)
                if len(data) == 50:
                    print("Loaded 50 results from benchmark_results.json")
                    return data
        except Exception as e:
            print("Error loading benchmark_results.json:", e)

    print("Executing 50 cases live against server on port 8005...")
    results = []
    for i, c in enumerate(CASES):
        print(f"[{i+1:02d}/50] Case #{c['id']}...")
        res = run_case(BASE_URL, c)
        results.append(res)
        if i < len(CASES) - 1:
            time.sleep(4.5)
    with open("benchmark_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    return results

def build_pdf():
    results = load_or_fetch_results()

    doc = SimpleDocTemplate(
        WORKSPACE_PDF,
        pagesize=landscape(letter),
        rightMargin=0.35 * inch,
        leftMargin=0.35 * inch,
        topMargin=0.4 * inch,
        bottomMargin=0.4 * inch
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#0F172A'),
        spaceAfter=4
    )

    subtitle_style = ParagraphStyle(
        'DocSubTitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=13,
        textColor=colors.HexColor('#475569'),
        spaceAfter=12
    )

    table_header_style = ParagraphStyle(
        'TableHeader',
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.white,
        alignment=1 # Center
    )

    cell_style = ParagraphStyle(
        'TableCell',
        fontName='Helvetica',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor('#1E293B')
    )

    cell_bold_style = ParagraphStyle(
        'TableCellBold',
        fontName='Helvetica-Bold',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor('#0F172A')
    )

    reasoning_style = ParagraphStyle(
        'ReasoningCell',
        fontName='Helvetica-Oblique',
        fontSize=7,
        leading=9,
        textColor=colors.HexColor('#334155')
    )

    elements = []

    # Title & Subtitle
    elements.append(Paragraph("ControlPlane AI Guardrail Benchmark Report", title_style))
    elements.append(Paragraph(f"Model Under Test: <b>gemini-3.1-flash-lite</b> &nbsp;|&nbsp; Target Endpoint: <code>http://127.0.0.1:8005/api/inspect</code> &nbsp;|&nbsp; Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}", subtitle_style))

    # Summary Statistics Banner Table
    fully_ok = sum(1 for r in results if r["decision_ok"] and r["checks_ok"])
    llm_calls = sum(1 for r in results if "llm-judge" in (r.get("performance_method") or ""))
    acc_pct = round((fully_ok / len(results)) * 100, 1)

    summary_data = [
        [
            Paragraph(f"<b>Total Cases Evaluated:</b><br/><font size=13 color='#0F172A'><b>{len(results)}</b></font>", cell_style),
            Paragraph(f"<b>Overall Accuracy:</b><br/><font size=13 color='#059669'><b>{fully_ok}/{len(results)} ({acc_pct}%)</b></font>", cell_style),
            Paragraph(f"<b>Live LLM Judge Calls:</b><br/><font size=13 color='#2563EB'><b>{llm_calls}/{len(results)}</b></font>", cell_style),
            Paragraph(f"<b>Rate Limit Fallbacks:</b><br/><font size=13 color='#D97706'><b>{len(results)-llm_calls}</b></font>", cell_style),
        ]
    ]

    summary_table = Table(summary_data, colWidths=[2.5*inch, 2.5*inch, 2.5*inch, 2.5*inch])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8FAFC')),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#E2E8F0')),
        ('INNERGRID', (0,0), (-1,-1), 1, colors.HexColor('#E2E8F0')),
        ('PADDING', (0,0), (-1,-1), 8),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))

    elements.append(summary_table)
    elements.append(Spacer(1, 12))

    # Telemetry Table Columns
    headers = [
        Paragraph("ID", table_header_style),
        Paragraph("Use Case", table_header_style),
        Paragraph("Actual", table_header_style),
        Paragraph("LLM Call Status", table_header_style),
        Paragraph("Resp", table_header_style),
        Paragraph("Perf", table_header_style),
        Paragraph("Cost", table_header_style),
        Paragraph("Tot", table_header_style),
        Paragraph("LLM Response Reasoning", table_header_style),
        Paragraph("Decision / Override Explanation", table_header_style),
    ]

    col_widths = [0.4*inch, 0.85*inch, 0.75*inch, 1.15*inch, 0.45*inch, 0.45*inch, 0.45*inch, 0.45*inch, 2.6*inch, 2.6*inch]

    table_rows = [headers]

    for r in results:
        actual = r.get('actual', '—')

        # Color pills for decisions
        if actual == 'PASS':
            actual_p = Paragraph(f"<font color='#059669'><b>PASS</b></font>", cell_bold_style)
        elif actual == 'BLOCK':
            actual_p = Paragraph(f"<font color='#DC2626'><b>BLOCK</b></font>", cell_bold_style)
        elif actual == 'FIX':
            actual_p = Paragraph(f"<font color='#D97706'><b>FIX</b></font>", cell_bold_style)
        else:
            actual_p = Paragraph(f"<font color='#2563EB'><b>HUMAN</b></font>", cell_bold_style)

        llm_status = "YES (llm-judge)" if "llm-judge" in (r.get("performance_method") or "") else "NO (fallback)"
        llm_p = Paragraph(f"<font color='#059669'><b>YES</b></font>" if "YES" in llm_status else f"<font color='#D97706'>NO (fallback)</font>", cell_style)

        resp_s = str(r.get('responsibility_score', '—'))
        perf_s = str(r.get('performance_score', '—'))
        cost_s = str(r.get('cost_score', '—'))
        tot_s = str(r.get('total_score', '—'))

        reasoning = (r.get("performance_reasoning") or "No reasoning")
        override = (r.get("override_reason") or "No override (score decision)")

        row = [
            Paragraph(f"#{r['id']}", cell_bold_style),
            Paragraph(r['use_case'], cell_style),
            actual_p,
            llm_p,
            Paragraph(resp_s, cell_style),
            Paragraph(perf_s, cell_style),
            Paragraph(cost_s, cell_style),
            Paragraph(tot_s, cell_bold_style),
            Paragraph(reasoning, reasoning_style),
            Paragraph(override, cell_style),
        ]
        table_rows.append(row)

    telemetry_table = Table(table_rows, colWidths=col_widths, repeatRows=1)
    
    # Table Styling
    t_style = [
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0F172A')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('ALIGN', (0,0), (0,-1), 'CENTER'),
        ('ALIGN', (4,0), (7,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#CBD5E1')),
        ('PADDING', (0,0), (-1,-1), 4),
    ]

    # Alternate row background colors
    for i in range(1, len(table_rows)):
        bg = colors.HexColor('#F8FAFC') if i % 2 == 0 else colors.white
        t_style.append(('BACKGROUND', (0, i), (-1, i), bg))

    telemetry_table.setStyle(TableStyle(t_style))
    elements.append(telemetry_table)

    doc.build(elements)
    print(f"Successfully generated PDF report: {WORKSPACE_PDF}")
    try:
        import shutil
        shutil.copy(WORKSPACE_PDF, ARTIFACT_PDF)
        print(f"Successfully copied PDF report to artifacts: {ARTIFACT_PDF}")
    except Exception as e:
        print("Copy to artifacts failed:", e)

if __name__ == "__main__":
    build_pdf()
