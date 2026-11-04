def input_requirements():
    return {
        "version": "pending-v1",
        "profiles": ["H-STD", "M-STD"],
        "comparisons": {
            "R-01": ["contract_rate", "slip_rate"],
            "R-02": ["worked_minutes", "slip_minutes"],
            "R-03": ["earnings", "gross"],
            "R-04": ["deductions", "deduction"],
            "R-05": ["gross", "deduction", "net"],
            "R-06": ["net", "transaction_allocations"],
        },
        "wage_context_requirements": None,
        "wage_rules_verified": False,
    }
