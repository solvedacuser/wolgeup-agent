"""Expand compact model rows without changing public extraction field contracts."""

import re
import unicodedata

from agent.providers.base import ExtractionResponse, TableExtractionResponse
from agent.schemas.public.errors import ModelFormatError
from agent.services.extraction_profiles import PROFILES
from agent.services.normalization import (
    DURATION_UNIT_PATTERN,
    HOUR_UNIT_PATTERN,
    normalize_evidenced_scalar,
    normalize_scalar,
)


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
    if raw is None:
        return raw, normalized, []
    value = compact_text(raw)
    # A scalar such as 100 or 8 must not borrow a substring of 100000 or 18:00.
    left = r"(?<![\d.,/+-])(?<!\d:)" if value and value[0] in "+-0123456789" else ""
    right = r"(?![\d.,/+-])(?!:\d)" if value and value[-1].isdigit() else ""
    present = bool(value) and (
        re.search(left + re.escape(value) + right, row_quote) if unit != "text" else value in row_quote
    )
    if present:
        return raw, normalized, []
    if unit == "minute":
        annotation = re.fullmatch(
            rf"(\d[\d,]*(?:\.\d+)?)\s*({DURATION_UNIT_PATTERN})\s*(?:\((h|hours?|min|minutes?)\))?",
            raw.strip(),
            re.I,
        )
        if annotation:
            number, source_unit, alias = annotation.groups()
            is_hour = re.fullmatch(HOUR_UNIT_PATTERN, source_unit, re.I) is not None
            if alias and (alias.lower() in ("h", "hour", "hours")) != is_hour:
                raise ModelFormatError(
                    "Table cell has conflicting unit annotations",
                    reason="TABLE_CELL_SOURCE_MISMATCH",
                    field_id=field_id,
                )
            if re.fullmatch(r"시\s*간", source_unit):
                spelling = re.search(
                    rf"(?<![\d.,/+\-−])(?<!\d:){re.escape(number)}\s*시\s*간(?![a-z가-힣])",
                    row_quote,
                )
                if spelling and not re.search(r"[+\-−]\s*\(?\s*$", row_quote[: spelling.start()]):
                    if normalized is None or normalize_scalar(spelling.group(), "minute") == normalized:
                        return spelling.group(), normalized, ["SOURCE_SCALAR_SPELLING_RESTORED"]
            # The number must be a cell scalar on this row, not part of a date or clock time.
            if re.search(rf"(?<![\d.,/+-])(?<!\d:){re.escape(number)}(?![\d.,/+-])(?!:\d)", row_quote):
                try:
                    annotated_value = normalize_scalar(number + source_unit, "minute")
                    evidenced_value = normalize_evidenced_scalar(
                        number, "minute", evidence_quote, field_id=field_id
                    )
                except ValueError, ArithmeticError:
                    pass
                else:
                    if annotated_value == evidenced_value == normalized:
                        return number, normalized, ["SOURCE_UNIT_FROM_HEADER"]
    if unit == "text":
        # A model may join adjacent source cells as '목, 정상근무'. Preserve the real source span.
        joined = " ".join(re.split(r"[\s,，·]+", compact_text(raw)))
        normalized_joined = " ".join(re.split(r"[\s,，·]+", compact_text(normalized or "")))
        if joined and joined in row_quote and normalized_joined == joined:
            return joined, joined, ["SOURCE_TEXT_SEPARATOR_NORMALIZED"]
    # The expanded quote also contains earlier rows. They may supply unit context,
    # but never a value missing from this row's own quoted source span.
    raise ModelFormatError(
        "Table cell value does not match its row",
        reason="TABLE_CELL_SOURCE_MISMATCH",
        field_id=field_id,
    )


def unused_row_span(page, quote, start, used):
    """Allocate each real source occurrence once, preserving genuine repeated rows."""
    position = page.find(quote, start)
    while position >= 0:
        end = position + len(quote)
        if all(end <= begin or position >= stop for begin, stop in used):
            used.append((position, end))
            return position
        position = page.find(quote, position + 1)
    raise ModelFormatError(
        "Table row has no unused matching source occurrence", reason="TABLE_ROW_SOURCE_REUSED"
    )


def flat_row_spans(fields, profile, pages, image_pages):
    """Apply the same source-occurrence limit to the older per-cell row format."""
    groups, used_rows, used_quotes = {}, {}, {}
    for item in fields:
        match = re.fullmatch(r"([a-z]+)_([1-9]\d*)_[a-z_]+", item.field_id)
        if not match or match[1] not in profile["rows"] or item.raw_value is None:
            continue
        if not 1 <= item.page <= len(pages):
            raise ModelFormatError("Evidence page outside document")
        page = compact_text(pages[item.page - 1])
        if not page or (image_pages or [False] * len(pages))[item.page - 1]:
            continue
        quote = compact_text(item.quote)
        if quote:
            groups.setdefault((match[1], match[2], item.page), set()).add(quote)
    for (prefix, _, page_number), quotes in groups.items():
        page = compact_text(pages[page_number - 1])
        spans = []
        for quote in sorted(quotes, key=len, reverse=True):
            # Legacy cells may use different overlapping context spans. Limit
            # reuse of each exact quote while allowing such valid context.
            used = used_quotes.setdefault((prefix, page_number, quote), [])
            position = unused_row_span(page, quote, 0, used)
            spans.append((position, position + len(quote)))
        used_rows.setdefault((prefix, page_number), []).extend(spans)
    return used_rows


def expand_tables(response, pages, image_pages=None):
    has_tables = "tables" in response
    try:
        parsed = (TableExtractionResponse if has_tables else ExtractionResponse).model_validate(response)
    except Exception as exc:
        raise ModelFormatError("Invalid table extraction schema") from exc
    profile = PROFILES.get(parsed.document_type)
    if not profile:
        raise ModelFormatError("Unknown document type")
    used_rows = flat_row_spans(parsed.fields, profile, pages, image_pages)
    if not has_tables:
        return response
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
                if not quote or begin < 0:
                    raise ModelFormatError(
                        "Table header or row does not match page text", reason="TABLE_SOURCE_MISMATCH"
                    )
                # Keep this across separate tables: splitting a table must not let
                # a model return the same source row twice under a new row ID.
                used = used_rows.setdefault((table.prefix, row.page), [])
                end = unused_row_span(page, quote, max(header_end, 0), used)
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


def expand_tables_for_review(response, pages, image_pages=None):
    """Expand identifiable rows, quarantining their errors outside the model data.

    The model cannot create the returned reason map. A damaged row remains in
    the candidate list so downstream completeness checks cannot overlook it.
    """
    has_tables = "tables" in response
    try:
        parsed = (TableExtractionResponse if has_tables else ExtractionResponse).model_validate(response)
    except Exception as exc:
        raise ModelFormatError("Invalid table extraction schema") from exc
    profile = PROFILES.get(parsed.document_type)
    if not profile:
        raise ModelFormatError("Unknown document type")
    result = parsed.model_dump(exclude={"tables"})
    reasons, used_rows, used_quotes, groups, model_values = {}, {}, {}, {}, {}

    def mark(field_ids, reason):
        for field_id in field_ids:
            reasons.setdefault(field_id, []).append(reason)

    for item in parsed.fields:
        match = re.fullmatch(r"([a-z]+)_([1-9]\d*)_[a-z_]+", item.field_id)
        if match and match[1] in profile["rows"]:
            groups.setdefault((match[1], match[2], item.page), []).append(item)
    for (prefix, _, page_number), items in groups.items():
        ids = [item.field_id for item in items]
        if not 1 <= page_number <= len(pages):
            mark(ids, "SOURCE_PAGE_INVALID")
            continue
        page = compact_text(pages[page_number - 1])
        if not page or (image_pages or [False] * len(pages))[page_number - 1]:
            continue
        for quote in sorted(
            {compact_text(item.quote) for item in items if item.raw_value is not None}, key=len, reverse=True
        ):
            try:
                if not quote:
                    raise ModelFormatError(reason="TABLE_SOURCE_MISMATCH")
                used = used_quotes.setdefault((prefix, page_number, quote), [])
                position = unused_row_span(page, quote, 0, used)
                used_rows.setdefault((prefix, page_number), []).append((position, position + len(quote)))
            except ModelFormatError as exc:
                mark(ids, exc.reason)

    row_counts = {}
    for table in getattr(parsed, "tables", []):
        definition = profile["rows"].get(table.prefix)
        if (
            not definition
            or not table.columns
            or len(table.columns) != len(set(table.columns))
            or any(key not in definition["fields"] for key in table.columns)
        ):
            # Column identities cannot be mapped safely; this is a malformed envelope.
            raise ModelFormatError("Unknown table or invalid table columns")
        for row in table.rows:
            row_counts[table.prefix] = row_counts.get(table.prefix, 0) + 1
            index = row_counts[table.prefix]
            ids = [f"{table.prefix}_{index}_{key}" for key in table.columns]
            for offset, field_id in enumerate(ids):
                model_values.setdefault(
                    field_id,
                    {
                        "suggested_value": row.normalized_values[offset]
                        if offset < len(row.normalized_values)
                        else None,
                        "source_quote": row.quote or None,
                        "source_page": row.page,
                    },
                )
            try:
                if not 1 <= row.page <= len(pages):
                    raise ModelFormatError(reason="SOURCE_PAGE_INVALID")
                if len(row.raw_values) != len(table.columns) or len(row.normalized_values) != len(
                    table.columns
                ):
                    raise ModelFormatError(reason="TABLE_SHAPE_INVALID")
                page, row_quote = compact_text(pages[row.page - 1]), compact_text(row.quote)
                quote, header = row_quote, compact_text(table.header_quote)
                if page and not (image_pages or [False] * len(pages))[row.page - 1]:
                    begin, start = header_span(table.header_quote, page) if header else (page.find(quote), 0)
                    if not quote or begin < 0:
                        raise ModelFormatError(reason="TABLE_SOURCE_MISMATCH")
                    position = unused_row_span(
                        page, quote, start, used_rows.setdefault((table.prefix, row.page), [])
                    )
                    quote = page[begin : position + len(quote)]
                elif header:
                    quote = header + "\n" + quote
                for key, field_id, raw, normalized in zip(
                    table.columns, ids, row.raw_values, row.normalized_values
                ):
                    unit = definition["fields"][key]["unit"]
                    cell_issues = []
                    try:
                        raw, normalized, cell_issues = source_cell(
                            raw, normalized, unit, field_id, row_quote, quote
                        )
                    except ModelFormatError as exc:
                        mark([field_id], exc.reason)
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
            except ModelFormatError as exc:
                reason = exc.reason or "TABLE_SHAPE_INVALID"
                mark(ids, reason)
                for offset, (key, field_id) in enumerate(zip(table.columns, ids)):
                    raw = row.raw_values[offset] if offset < len(row.raw_values) else None
                    normalized = (
                        row.normalized_values[offset] if offset < len(row.normalized_values) else None
                    )
                    result["fields"].append(
                        {
                            "field_id": field_id,
                            "raw_value": raw,
                            "normalized_value": normalized,
                            "unit": definition["fields"][key]["unit"] if raw is not None else None,
                            "page": row.page,
                            "quote": row.quote,
                            "issues": list(row.issues),
                        }
                    )
    return result, {key: list(dict.fromkeys(value)) for key, value in reasons.items()}, model_values
