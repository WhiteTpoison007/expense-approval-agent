from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ExpenseApprovalMCP")

@mcp.tool()
def get_employee_policy_limit(employee_role: str) -> float:
    """Get the standard expense limit for a given employee role.
    
    Args:
        employee_role: The role of the employee (e.g. Executive, Manager, Engineer).
    """
    role = employee_role.lower()
    if "exec" in role:
        return 2000.0
    elif "mgr" in role or "manager" in role:
        return 1000.0
    elif "engineer" in role or "developer" in role:
        return 500.0
    return 300.0

@mcp.tool()
def lookup_vendor_risk(vendor_name: str) -> str:
    """Checks the risk status of a vendor.
    
    Args:
        vendor_name: The name of the vendor to lookup.
    """
    vendor = vendor_name.lower()
    high_risk_vendors = ["unknown cash advance", "shell company inc", "shady enterprise"]
    medium_risk_vendors = ["crypto coin exchange", "anonymous giftcard shop"]
    
    if any(h in vendor for h in high_risk_vendors):
        return f"HIGH RISK: Vendor '{vendor_name}' is flagged on the corporate high-risk vendor watchlist."
    elif any(m in vendor for m in medium_risk_vendors):
        return f"MEDIUM RISK: Vendor '{vendor_name}' requires additional receipt verification."
    return "LOW RISK: Vendor is in good standing."

@mcp.tool()
def query_previous_expenses(employee_name: str) -> str:
    """Retrieves previous expense records for an employee to detect duplicates or patterns.
    
    Args:
        employee_name: Name of the employee to check history for.
    """
    mock_history = [
        {"date": "2026-06-10", "category": "Meals", "amount": 85.0, "vendor": "Downtown Steakhouse", "description": "Client dinner"},
        {"date": "2026-06-15", "category": "Software", "amount": 49.0, "vendor": "GitHub Inc", "description": "Copilot subscription"},
        {"date": "2026-06-20", "category": "Travel", "amount": 240.0, "vendor": "Grand Hyatt", "description": "Conference lodging"}
    ]
    return f"Recent expenses for {employee_name}:\n" + "\n".join(
        f"- {item['date']} | {item['category']} | ${item['amount']} at {item['vendor']} ({item['description']})"
        for item in mock_history
    )

if __name__ == "__main__":
    mcp.run()
