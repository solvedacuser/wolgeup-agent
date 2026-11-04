def filter_institutions(records, *, region=None, field=None, language=None):
    """Filter an already verified directory; never invent missing details."""
    return [
        dict(record)
        for record in records
        if record.get("official_url")
        and record.get("verified_at")
        and (region is None or record.get("region") in (region, "전국"))
        and (field is None or field in record.get("fields", []))
        and (language is None or language in record.get("languages", []))
    ]
