def affected_cases(changes, case_dependencies):
    """Return affected case IDs from supplied data; no cross-case DB exploration."""
    changed = set(changes)
    return sorted(
        case_id for case_id, dependencies in case_dependencies.items() if changed.intersection(dependencies)
    )
