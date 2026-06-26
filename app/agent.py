# ruff: noqa: E501
import os
import re
import json
import logging
import sys
from typing import List, Literal, Any

from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool
from google.adk.workflow import Workflow, FunctionNode, JoinNode, START, node, RetryConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App
from google.genai import types
from pydantic import BaseModel, Field

from mcp import StdioServerParameters
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams

from .config import config

# Resolve the absolute path of mcp_server.py to avoid pathing errors
mcp_server_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "mcp_server.py"))

mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[mcp_server_path],
        ),
        timeout=60,
    )
)

# ---------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------

class ExpenseItem(BaseModel):
    category: str = Field(description="Expense category: e.g. Meals, Lodging, Flights, Subscriptions")
    amount: float = Field(description="Amount of the expense item in USD")
    description: str = Field(description="Short description / details of the item")

class ExpenseSubmission(BaseModel):
    employee_name: str = Field(description="Name of the employee submitting the expense")
    employee_role: str = Field(description="Role of the employee (Executive, Manager, Engineer)")
    items: List[ExpenseItem] = Field(description="List of expense items")

class SubAgentFeedback(BaseModel):
    findings: List[str] = Field(description="Compliance observations, policy limits exceeded, or anomalies found")
    status: Literal["OK", "FLAGGED"] = Field(description="Compliance status of the check")

class ExpenseAuditReport(BaseModel):
    employee_name: str = Field(description="Name of the employee")
    total_amount: float = Field(description="Total calculated amount")
    policy_violations: List[str] = Field(default_factory=list, description="List of policy violations")
    fraud_flags: List[str] = Field(default_factory=list, description="List of suspected fraud indicators")
    recommendation: Literal["APPROVED", "DENIED", "NEEDS_REVIEW"] = Field(description="Recommendation status")
    explanation: str = Field(description="Detailed explanation of the recommendation")

# ---------------------------------------------------------
# Orchestrator & Report Formatter Definitions
# ---------------------------------------------------------

orchestrator = LlmAgent(
    name="orchestrator",
    model=config.model,
    instruction="""You are the main expense approval orchestrator.
Review the following expense submission: {expense_submission}.

You must perform two checks:
1. Policy Audit: Inspect the expense items and verify if they violate standard company policies. Standard limits:
   - Meals/Entertainment: max $100 per receipt.
   - Travel/Lodging: max $250 per night.
   - Flights: must be economy class.
   - Software subscriptions: max $50 per month.
   Use the `get_employee_policy_limit` tool to check if the employee's role has specific limit overrides, and check their historical expenses via `query_previous_expenses`.
2. Fraud Detection: Check the items for suspicious duplicate claims or vendor risks. Use the `query_previous_expenses` tool to detect identical receipts in the past and check vendor reputations via `lookup_vendor_risk`.

Summarize your findings in detail, including:
- The employee name and role.
- The total amount of the expense.
- All policy violations found (if any).
- All suspected fraud flags found (if any).
- Your overall recommendation and explanation.

Be detailed and thorough in your text notes so the downstream formatter can structure it correctly.""",
    tools=[mcp_toolset],
    output_key="orchestrator_notes",
)

report_formatter = LlmAgent(
    name="report_formatter",
    model=config.model,
    instruction="""Analyze the audit notes from the orchestrator and extract them into a structured ExpenseAuditReport.
Notes to analyze: {orchestrator_notes}

Ensure you extract:
- employee_name
- total_amount
- policy_violations (list of strings)
- fraud_flags (list of strings)
- recommendation: APPROVED (if total <= 500, no violations, no fraud), DENIED, or NEEDS_REVIEW (if total > 500, or has violations/flags needing review)
- explanation: brief justification of the decision.""",
    output_schema=ExpenseAuditReport,
    output_key="audit_report",
)

# ---------------------------------------------------------
# Workflow Node Functions & Audit Logging
# ---------------------------------------------------------

import datetime

def audit_log(event_type: str, severity: str, details: dict):
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "event_type": event_type,
        "severity": severity,
        "details": details
    }
    print(f"AUDIT_LOG: {json.dumps(log_entry)}", flush=True)

def has_pii(text: str) -> bool:
    ssn_pattern = r'\b\d{3}-\d{2}-\d{4}\b'
    cc_pattern = r'\b(?:\d[ -]*?){13,16}\b'
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    return bool(re.search(ssn_pattern, text) or re.search(cc_pattern, text) or re.search(email_pattern, text))

def has_injection(text: str) -> bool:
    keywords = ["ignore previous instructions", "bypass system", "override policy", "forget your system prompt", "you are now a sudo"]
    for kw in keywords:
        if kw in text.lower():
            return True
    return False

def security_checkpoint(ctx: Context, node_input: ExpenseSubmission) -> Event:
    raw_data = json.dumps(node_input.model_dump())
    
    if config.pii_redaction_enabled and has_pii(raw_data):
        audit_log("SECURITY_VIOLATION", "WARNING", {"reason": "PII Detected", "employee": node_input.employee_name})
        return Event(
            route="SECURITY_EVENT",
            state={"security_error": "PII Detected: Submissions must not contain sensitive info (SSN, credit card, personal emails)."}
        )
        
    if config.injection_detection_enabled and has_injection(raw_data):
        audit_log("SECURITY_VIOLATION", "CRITICAL", {"reason": "Prompt Injection Attempt", "employee": node_input.employee_name})
        return Event(
            route="SECURITY_EVENT",
            state={"security_error": "Security Alert: Potential prompt injection attempt blocked."}
        )
        
    # Domain-Specific Rule: Validate employee role & limit total items
    valid_roles = ["executive", "manager", "engineer", "developer"]
    if node_input.employee_role.lower() not in valid_roles:
        audit_log("POLICY_VIOLATION", "WARNING", {"reason": "Invalid Employee Role", "role": node_input.employee_role})
        return Event(
            route="SECURITY_EVENT",
            state={"security_error": f"Validation Error: Employee role '{node_input.employee_role}' is not recognized as a valid corporate role."}
        )
        
    if not node_input.items:
        audit_log("POLICY_VIOLATION", "WARNING", {"reason": "Empty items list", "employee": node_input.employee_name})
        return Event(
            route="SECURITY_EVENT",
            state={"security_error": "Validation Error: Expense submission must contain at least one line item."}
        )
        
    audit_log("SECURITY_CHECK_PASSED", "INFO", {"employee": node_input.employee_name, "role": node_input.employee_role})
    return Event(output=raw_data, route="OK", state={"expense_submission": raw_data})

def decision_router(ctx: Context, node_input: dict) -> Event:
    rec = node_input.get("recommendation", "NEEDS_REVIEW")
    total = node_input.get("total_amount", 0.0)
    ctx.state["audit_report"] = node_input
    
    audit_log("AUDIT_DECISION_ROUTING", "INFO", {"recommendation": rec, "total_amount": total})
    
    if rec == "APPROVED" and total <= 500:
        audit_log("AUTO_APPROVAL", "INFO", {"employee": node_input.get("employee_name"), "total": total})
        return Event(output=node_input, route="APPROVED")
    elif rec == "DENIED":
        audit_log("AUTO_REJECTION", "WARNING", {"employee": node_input.get("employee_name"), "total": total})
        return Event(output=node_input, route="DENIED")
    else:
        audit_log("HITL_REVIEW_TRIGGERED", "INFO", {"employee": node_input.get("employee_name"), "total": total})
        return Event(output=node_input, route="NEEDS_REVIEW")

async def human_approval(ctx: Context, node_input: dict):
    if not ctx.resume_inputs:
        audit_log("HITL_AWAITING_INPUT", "INFO", {"interrupt_id": "manager_decision"})
        yield RequestInput(
            interrupt_id="manager_decision",
            message=f"Expense audit flagged for review. Total amount: ${node_input.get('total_amount')}. Details: {node_input.get('explanation')}. Approve? (reply 'yes' or 'no')"
        )
        return
        
    decision = ctx.resume_inputs.get("manager_decision", "").strip().lower()
    report = ctx.state.get("audit_report", {})
    if "yes" in decision or "approve" in decision:
        report["recommendation"] = "APPROVED"
        report["explanation"] = f"Approved by manager. Original explanation: {report.get('explanation')}"
        audit_log("HITL_DECISION_RECORDED", "INFO", {"decision": "APPROVED", "manager_input": decision})
        yield Event(output=report, route="human_approved", state={"audit_report": report})
    else:
        report["recommendation"] = "DENIED"
        report["explanation"] = f"Denied by manager. Original explanation: {report.get('explanation')}"
        audit_log("HITL_DECISION_RECORDED", "WARNING", {"decision": "DENIED", "manager_input": decision})
        yield Event(output=report, route="human_denied", state={"audit_report": report})

def approval_handler(ctx: Context, node_input: dict):
    msg = f"✅ Expense Claim APPROVED\n\nEmployee: {node_input.get('employee_name')}\nTotal: ${node_input.get('total_amount')}\nExplanation: {node_input.get('explanation')}"
    audit_log("EXPENSE_APPROVED", "INFO", {"employee": node_input.get("employee_name"), "total": node_input.get("total_amount")})
    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)]),
        output=node_input
    )

def rejection_handler(ctx: Context, node_input: dict):
    msg = f"❌ Expense Claim DENIED\n\nEmployee: {node_input.get('employee_name')}\nTotal: ${node_input.get('total_amount')}\nExplanation: {node_input.get('explanation')}"
    audit_log("EXPENSE_REJECTED", "WARNING", {"employee": node_input.get("employee_name"), "total": node_input.get("total_amount")})
    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)]),
        output=node_input
    )

def security_event_handler(ctx: Context, node_input: Any):
    error_msg = ctx.state.get("security_error", "Security check failed.")
    msg = f"⚠️ SECURITY BLOCK\n\n{error_msg}"
    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)]),
        output={"status": "SECURITY_BLOCK", "error": error_msg}
    )

def final_output(ctx: Context, node_input: dict) -> dict:
    return node_input

# ---------------------------------------------------------
# Resilient Workflow Nodes
# ---------------------------------------------------------

orchestrator_node = node(orchestrator, retry_config=RetryConfig(max_attempts=3))
report_formatter_node = node(report_formatter, retry_config=RetryConfig(max_attempts=3))

# ---------------------------------------------------------
# Workflow Definition
# ---------------------------------------------------------

root_agent = Workflow(
    name="expense_approval_workflow",
    edges=[
        ('START', security_checkpoint),
        (security_checkpoint, {
            "SECURITY_EVENT": security_event_handler,
            "OK": orchestrator_node
        }),
        (orchestrator_node, report_formatter_node),
        (report_formatter_node, decision_router),
        (decision_router, {
            "NEEDS_REVIEW": human_approval,
            "APPROVED": approval_handler,
            "DENIED": rejection_handler
        }),
        (human_approval, {
            "human_approved": approval_handler,
            "human_denied": rejection_handler
        }),
        (approval_handler, final_output),
        (rejection_handler, final_output),
        (security_event_handler, final_output),
    ],
    input_schema=ExpenseSubmission,
)

app = App(
    root_agent=root_agent,
    name="app",
)
