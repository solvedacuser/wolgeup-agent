"""Review routes kept separate from the existing document console API."""

import re
from pathlib import Path
from urllib.parse import unquote

from agent.review.reading import png_bytes
from agent.review.registry import profiles

WEB = Path(__file__).parent / "web"


def route(handler, method, path):
    reviews, call = handler.server.store.reviews, handler.server.call
    if method == "GET" and (
        path == "/"
        or re.fullmatch(r"/review(?:/(?:new|library|review-[a-f0-9]+/(?:materials|verify)))?", path)
    ):
        handler.respond(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        return True
    if method == "GET" and path in (
        "/review/app.js",
        "/review/recommendations.js",
        "/review/chat.js",
        "/review/chat-host.js",
        "/review/styles.css",
        "/review/chat.css",
    ):
        name = path.rsplit("/", 1)[1]
        handler.respond(
            200,
            (WEB / name).read_bytes(),
            "text/javascript; charset=utf-8" if name.endswith("js") else "text/css; charset=utf-8",
        )
        return True
    context = re.fullmatch(r"/api/review-context/([a-z0-9-]+)", path)
    if method == "GET" and context:
        rid = next(
            (
                r["id"]
                for r in reviews.records.values()
                if any(q.get("comparison_id") == context[1] for q in r["requests"])
            ),
            None,
        )
        handler.respond(200, {"review_id": rid})
        return True
    if path == "/api/reviews":
        if method == "GET":
            handler.respond(
                200,
                {"reviews": reviews.summaries(), "workplaces": reviews.workplaces(), "profiles": profiles()},
            )
        elif method == "POST":
            handler.body()
            handler.respond(201, call(reviews.create()))
        else:
            return False
        return True
    original = re.fullmatch(r"/api/documents/([a-z0-9-]+)/original", path)
    if method == "GET" and original:
        store = handler.server.store
        metadata = store.documents[original[1]]
        handler.respond(200, store.document_data(original[1]), metadata.get("media_type", "application/pdf"))
        return True
    match = re.fullmatch(
        r"/api/reviews/(review-[a-f0-9]+)(?:/(documents|fields|confirm|requests|link|chat|recommendations))?(?:/([a-z0-9-]+))?(?:/pages/(\d+)\.png)?",
        path,
    )
    if not match:
        return False
    rid, action, target, page = match.groups()
    review = reviews.records[rid]
    if action == "recommendations" and method == "POST":
        if target is None:
            handler.respond(202, call(reviews.recommendations.start(rid, handler.body())))
        elif target == "apply":
            handler.respond(200, call(reviews.recommendations.apply(rid, handler.body())))
        else:
            return False
        return True
    if action == "chat" and target is None:
        if method == "GET":
            handler.respond(200, reviews.chat.view(rid))
        elif method == "POST":
            handler.respond(202, call(reviews.chat.send(rid, handler.body())))
        else:
            return False
        return True
    if page and action == "documents" and method == "GET":
        document = reviews.document(review, target)
        page = int(page)
        if not 1 <= page <= document["pages"]:
            raise ValueError("페이지 범위를 확인해 주세요.")
        data = handler.server.store.document_data(target)
        handler.respond(200, png_bytes(data, document["media_type"], page), "image/png")
        return True
    if method == "GET" and action is None:
        handler.respond(200, reviews.view(rid))
        return True
    if method == "PUT" and action is None:
        result = call(reviews.update(rid, handler.body()))
    elif action == "documents" and method == "POST" and target is None:
        result = call(
            reviews.upload(
                rid, handler.body(binary=True), unquote(handler.headers.get("X-Document-Name", "자료"))
            )
        )
    elif action == "documents" and method == "POST" and target:
        result = call(reviews.document_action(rid, target, handler.body()))
    elif action == "fields" and method == "PUT":
        result = call(reviews.edit_field(rid, handler.body()))
    elif action == "fields" and method == "POST":
        result = call(reviews.add_fields(rid, handler.body()))
    elif action == "confirm" and method == "POST":
        result = call(reviews.confirm(rid, handler.body()))
    elif action == "requests" and method == "POST" and target is None:
        result = call(reviews.request(rid, handler.body()))
    elif action == "requests" and method == "POST" and target:
        handler.body()
        result = call(reviews.cancel_request(rid, target))
    elif action == "link" and method == "POST":
        result = call(reviews.link(rid, handler.body()))
    else:
        return False
    handler.respond(201 if action == "documents" and target is None else 200, result)
    return True
