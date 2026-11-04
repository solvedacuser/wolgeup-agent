"""Expand compact model rows without changing public extraction field contracts."""

import re
import unicodedata

from agent.providers.base import TableExtractionResponse
from agent.schemas.public.errors import ModelFormatError
from agent.services.extraction_profiles import PROFILES
from agent.services.normalization import normalize_evidenced_scalar, normalize_scalar


def compact_text(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def header_span(header_quote, page):
    """Locate every quoted header line in order, retaining intervening source headings."""
    header = compact_text(header_quote)
    begin = page.find(header)
    if begin >= 0:
        return begin, begin + len(header)
    lines = [compact_text(line) for line in header_quote.splitlines() if compact_text(line)]
    if len(lines) < 2:
        raise ModelFormatError("Table header does not match page text", reason="TABLE_SOURCE_MISMATCH")
    begin, cursor = None, 0
    for line in lines:
        position = page.find(line, cursor)
        if position < 0:
            raise ModelFormatError("Table header does not match page text", reason="TABLE_SOURCE_MISMATCH")
        if begin is None:
            begin = position
        cursor = position + len(line)
    return begin, cursor


def source_cell(raw, normalized, unit, field_id, row_quote, evidence_quote):
    """Recover source spelling only when the row and its verified units prove equivalence."""
    if raw is None or compact_text(raw) in row_quote:
        return raw, normalized, []
    if unit == "minute":
        annotation = re.fullmatch(
            r"(\d[\d,]*(?:\.\d+)?)\s*(시간|hours?|h|분|minutes?|min)\s*(?:\((h|hours?|min|minutes?)\))?",
            raw.strip(),
            re.I,
        )
        if annotation:
            number, source_unit, alias = annotation.groups()
            is_hour = source_unit.lower() in ("시간", "h", "hour", "hours")
            if alias and (alias.lower() in ("h", "hour", "hours")) != is_hour:
                return raw, normalized, []
            # The number must be a cell scalar on this row, not part of a date or clock time.
            if re.search(rf"(?<![\d.,:/]){re.escape(number)}(?![\d.,:/])", row_quote):
                try:
                    annotated_value = normalize_scalar(number + source_unit, "minute")
                    evidenced_value = normalize_evidenced_scalar(
                        number, "minute", evidence_quote, field_id=field_id
                    )
                except ValueError, ArithmeticError:
                    return raw, normalized, []
                if annotated_value == evidenced_value == normalized:
                    return number, normalized, ["SOURCE_UNIT_FROM_HEADER"]
    if unit == "text":
        # A model may join adjacent source cells as '목, 정상근무'. Preserve the real source span.
        joined = " ".join(re.split(r"[\s,，·]+", compact_text(raw)))
        normalized_joined = " ".join(re.split(r"[\s,，·]+", compact_text(normalized or "")))
        if joined and joined in row_quote and normalized_joined == joined:
            return joined, joined, ["SOURCE_TEXT_SEPARATOR_NORMALIZED"]
    return raw, normalized, []


def expand_tables(response, pages, image_pages=None):
    if "tables" not in response:
        return response
    try:
        parsed = TableExtractionResponse.model_validate(response)
    except Exception as exc:
        raise ModelFormatError("Invalid table extraction schema") from exc
    profile = PROFILES.get(parsed.document_type)
    if not profile:
        raise ModelFormatError("Unknown document type")
    result = parsed.model_dump(exclude={"tables"})
    row_counts = {}
    for table in parsed.tables:
        definition = profile["rows"].get(table.prefix)
        if not definition or len(table.columns) != len(set(table.columns)):
            raise ModelFormatError("Unknown table or duplicate table columns")
        if not table.columns or any(key not in definition["fields"] for key in table.columns):
            raise ModelFormatError("Unknown table column")
        for row in table.rows:
            if not 1 <= row.page <= len(pages):
                raise ModelFormatError("Table evidence page outside document")
            if len(row.raw_values) != len(table.columns) or len(row.normalized_values) != len(table.columns):
                raise ModelFormatError("Table cell count does not match columns")
            row_quote = compact_text(row.quote)
            quote = row_quote
            header = compact_text(table.header_quote)
            page = compact_text(pages[row.page - 1])
            is_image = (image_pages or [False] * len(pages))[row.page - 1]
            if page and not is_image:
                begin, header_end = header_span(table.header_quote, page) if header else (page.find(quote), 0)
                end = page.find(quote, max(header_end, 0))
                if not quote or begin < 0 or end < 0:
                    raise ModelFormatError(
                        "Table header or row does not match page text", reason="TABLE_SOURCE_MISMATCH"
                    )
                # Include the real, contiguous source span so header units remain evidence.
                quote = page[begin : end + len(quote)]
            elif header:
                quote = header + "\n" + quote
            row_counts[table.prefix] = row_counts.get(table.prefix, 0) + 1
            for key, raw, normalized in zip(table.columns, row.raw_values, row.normalized_values):
                field_id = f"{table.prefix}_{row_counts[table.prefix]}_{key}"
                unit = definition["fields"][key]["unit"]
                raw, normalized, cell_issues = source_cell(raw, normalized, unit, field_id, row_quote, quote)
                result["fields"].append(
                    {
                        "field_id": field_id,
                        "raw_value": raw,
                        "normalized_value": normalized,
                        "unit": unit if raw is not None else None,
                        "page": row.page,
                        "quote": quote if raw is not None else "",
                        "issues": list(row.issues) + cell_issues,
                    }
                )
    return result
