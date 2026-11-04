import re


class FixtureProvider:
    """Explicit synthetic provider; never used as an automatic fallback."""

    async def invoke(self, operation, data):
        if operation == "explain":
            return {"templates": ["확인 결과: {{summary}}"], "question_ids": data["question_ids"]}, {}
        fields = []
        for page, text in enumerate(data.get("pages", []), 1):
            for line in text.splitlines():
                match = re.match(r"([a-z_]+):\s*(\d+(?:\.\d+)?)\s*(\S+)", line.strip())
                if match:
                    name, value, unit = match.groups()
                    fields.append(
                        {
                            "field_id": name,
                            "raw_value": value,
                            "normalized_value": value,
                            "unit": unit,
                            "page": page,
                            "quote": line.strip(),
                            "issues": [],
                        }
                    )
        return {"document_type": data["document_type"], "fields": fields, "issues": []}, {}
